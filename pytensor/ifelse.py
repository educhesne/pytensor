"""
IfElse introduces lazy evaluation in PyTensor (coupled with the CVM/VM
linkers). It resembles the if clause of any programming language, that
has a `then` and `else` branch, and executes either one or the other
according to the condition provided.

This op differs from the already existent `switch` op, that evaluates both
branches of the clause and afterwards picks (according to the condition)
which value to report. Note also that `switch` is an elemwise operation (so
it picks each entry of a matrix according to the condition) while `ifelse`
is a global operation with a scalar condition.
"""

from collections.abc import Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import numpy as np

import pytensor.tensor as pt
from pytensor import as_symbolic
from pytensor.compile import optdb
from pytensor.configdefaults import config
from pytensor.graph.basic import Apply, Variable, apply_depends_on
from pytensor.graph.op import _NoPythonOp
from pytensor.graph.replace import clone_replace
from pytensor.graph.rewriting.basic import GraphRewriter, in2out, node_rewriter
from pytensor.graph.type import HasDataType, HasShape
from pytensor.tensor.shape import Reshape, Shape, SpecifyShape


if TYPE_CHECKING:
    from pytensor.tensor import TensorLike


class IfElse(_NoPythonOp):
    r"""An `Op` that provides conditional graph evaluation.

    According to a scalar condition, this `Op` evaluates and then
    returns all the tensors provided on the "then"-branch, otherwise it
    evaluates and returns the tensors provided on the "else"-branch. The `Op`
    supports multiple tensors on each branch, with the condition that the same
    number of tensors are on the "then"-branch as on the "else"-branch and
    there is a one to one correspondence between their dtypes and numbers of
    dimensions.

    The "then"-branch is defined as the first ``N`` tensors (after the
    condition), while the "else"-branch is defined as the last ``N`` tensors.

    Example usage:

    .. code-block::

        rval = ifelse(condition,
                      rval_if_true_1, ..., rval_if_true_N,
                      rval_if_false_1, ..., rval_if_false_N)

    .. note:

        `Linker`\s other than `CVM`, and some other `VM` subclasses, are
        incompatible with this `Op`, and will ignore its lazy characteristic,
        computing both the true and false branches before returning one.

    """

    __props__ = ("as_view", "n_outs")

    def __init__(self, n_outs, as_view=False, name=None):
        if as_view:
            # check destroyhandler and others to ensure that a view_map with
            # multiple inputs can work
            view_map = {}
            for idx in range(n_outs):
                view_map[idx] = [idx + 1]
            self.view_map = view_map
        self.as_view = as_view
        self.n_outs = n_outs
        self.name = name

    def __eq__(self, other):
        if type(self) is not type(other):
            return False
        if self.as_view != other.as_view:
            return False
        if self.n_outs != other.n_outs:
            return False
        return True

    def __hash__(self):
        return hash((type(self), self.as_view, self.n_outs))

    def __str__(self):
        args = []
        if self.name is not None:
            args.append(self.name)
        if self.as_view:
            args.append("inplace")
        return f"if{{{','.join(args)}}}"

    def infer_shape(self, fgraph, node, inputs_shapes):
        # By construction, corresponding then/else pairs have the same number
        # of dimensions

        ts_shapes = inputs_shapes[1:][: self.n_outs]
        fs_shapes = inputs_shapes[1:][self.n_outs :]
        # All elements of all shape tuples for the true and false outputs are
        # unpacked into the inputs of a separate ifelse, and then the outputs
        # of that ifelse are packed back into shape tuples.
        new_ts_inputs = []
        for ts_shape in ts_shapes:
            if isinstance(ts_shape, list | tuple):
                new_ts_inputs += list(ts_shape)
            else:
                # It can be None for generic objects
                return [None] * self.n_outs

        new_fs_inputs = []
        for fs_shape in fs_shapes:
            if isinstance(fs_shape, list | tuple):
                new_fs_inputs += list(fs_shape)
            else:
                # It can be None for generic objects
                return [None] * self.n_outs

        assert len(new_ts_inputs) == len(new_fs_inputs)
        if len(new_ts_inputs + new_fs_inputs) > 0:
            name_tokens = ["shape"]
            if self.name is not None:
                name_tokens.append(self.name)

            new_ifelse = IfElse(
                n_outs=len(new_ts_inputs),
                as_view=False,
                name="_".join(name_tokens),
            )
            new_outs = new_ifelse(
                node.inputs[0],
                *(new_ts_inputs + new_fs_inputs),
                return_list=True,
            )
        else:
            new_outs = []

        # generate pairs of shapes
        out_shapes = []
        for out in node.outputs:
            out_shapes.append(tuple(new_outs[: out.ndim]))
            new_outs = new_outs[out.ndim :]

        # new_outs should be an empty list after last iteration
        assert len(new_outs) == 0

        return out_shapes

    def make_node(self, condition: "TensorLike", *true_false_branches: Any):
        if len(true_false_branches) != 2 * self.n_outs:
            raise ValueError(
                f"Wrong number of arguments: expected "
                f"{int(2 * self.n_outs)}, got {len(true_false_branches)}"
            )

        condition = pt.basic.as_tensor_variable(condition)

        if condition.type.ndim > 0:
            raise TypeError("The condition argument must be a truthy scalar value")

        inputs_true_branch = true_false_branches[: self.n_outs]
        inputs_false_branch = true_false_branches[self.n_outs :]

        output_vars = []
        new_inputs_true_branch = []
        new_inputs_false_branch = []
        for input_t, input_f in zip(
            inputs_true_branch, inputs_false_branch, strict=True
        ):
            if not isinstance(input_t, Variable):
                input_t = as_symbolic(input_t)
            if not isinstance(input_f, Variable):
                input_f = as_symbolic(input_f)

            if type(input_f.type) != type(input_t.type):  # noqa: E721
                raise TypeError(
                    f"Input types {type(input_t.type)} and {type(input_f.type)} do not match."
                )

            if isinstance(input_t.type, HasDataType) and isinstance(
                input_f.type, HasDataType
            ):
                # TODO: Be smarter about dtype casting.
                # up_dtype = ps.upcast(input_t.type.dtype, input_f.type.dtype)

                if input_t.type.dtype != input_f.type.dtype:
                    raise TypeError(
                        "IfElse requires compatible dtypes for both branches: got "
                        f"true_branch={input_t.type.dtype}, false_branch={input_f.type.dtype}"
                    )

            if isinstance(input_t.type, HasShape) and isinstance(
                input_f.type, HasShape
            ):
                if input_t.type.ndim != input_f.type.ndim:
                    raise TypeError(
                        "IfElse requires compatible ndim values for both branches: got "
                        f"true_branch={input_t.type.ndim}, false_branch={input_f.type.ndim}"
                    )

                # We can only use static shape information that corresponds
                # in both branches, because the outputs of this `Op` are
                # allowed to have distinct shapes from either branch
                new_shape = tuple(
                    s_t if s_t == s_f else None
                    for s_t, s_f in zip(
                        input_t.type.shape, input_f.type.shape, strict=True
                    )
                )
                # TODO FIXME: The presence of this keyword is a strong
                # assumption.  Find something that's guaranteed by the/a
                # confirmed interface.
                output_var_t = input_t.type.clone(shape=new_shape)()
                output_var_f = input_f.type.clone(shape=new_shape)()
            else:
                output_var_t = input_t.type()
                output_var_f = input_f.type()

            input_t_ = output_var_f.type.filter_variable(input_t)
            input_f_ = output_var_t.type.filter_variable(input_f)

            new_inputs_true_branch.append(input_t_)
            new_inputs_false_branch.append(input_f_)
            output_vars.append(output_var_t)

        return Apply(
            self,
            [condition, *new_inputs_true_branch, *new_inputs_false_branch],
            output_vars,
        )

    def R_op(self, inputs, eval_points):
        return self(inputs[0], *eval_points[1:], return_list=True)

    def grad(self, ins, grads):
        condition = ins[0]
        inputs_true_branch = ins[1:][: self.n_outs]
        inputs_false_branch = ins[1:][self.n_outs :]

        if self.name is not None:
            nw_name_t = self.name + "_grad_t"
            nw_name_f = self.name + "_grad_f"
        else:
            nw_name_t = None
            nw_name_f = None

        if_true_op = IfElse(n_outs=self.n_outs, as_view=self.as_view, name=nw_name_t)
        if_false_op = IfElse(n_outs=self.n_outs, as_view=self.as_view, name=nw_name_f)

        # The `grads` can have different dtypes than the `inputs`.
        # Since input true/false entries must have the same dtypes, we need to
        # cast the zeros to the corresponding `grads` dtypes and not the input
        # dtypes.
        inputs_true_grad = (
            [condition]
            + grads
            + [
                pt.basic.zeros_like(t, dtype=grads[i].dtype)
                for i, t in enumerate(inputs_true_branch)
            ]
        )
        inputs_false_grad = (
            [condition]
            + [
                pt.basic.zeros_like(f, dtype=grads[i].dtype)
                for i, f in enumerate(inputs_false_branch)
            ]
            + grads
        )

        # `condition` does affect the elements of the output so it is connected.
        # For the sake of making the gradient convenient we assume that
        # condition + epsilon always triggers the same branch as condition
        condition_grad = condition.zeros_like(dtype=config.floatX)

        return [
            condition_grad,
            *if_true_op(*inputs_true_grad, return_list=True),
            *if_false_op(*inputs_false_grad, return_list=True),
        ]

    def make_thunk(self, node, storage_map, compute_map, no_recycling, impl=None):
        cond = node.inputs[0]
        input_true_branch = node.inputs[1:][: self.n_outs]
        inputs_false_branch = node.inputs[1:][self.n_outs :]
        outputs = node.outputs

        def thunk():
            if not compute_map[cond][0]:
                return [0]
            else:
                truthval = storage_map[cond][0]
                if truthval != 0:
                    ls = [
                        idx + 1
                        for idx in range(self.n_outs)
                        if not compute_map[input_true_branch[idx]][0]
                    ]
                    if len(ls) > 0:
                        return ls
                    else:
                        # strict=False because we are in a hot loop
                        for out, t in zip(outputs, input_true_branch, strict=False):
                            compute_map[out][0] = 1
                            val = storage_map[t][0]
                            if self.as_view:
                                storage_map[out][0] = val
                            # Work around broken numpy deepcopy
                            elif isinstance(val, np.ndarray | np.memmap):
                                storage_map[out][0] = val.copy()
                            else:
                                storage_map[out][0] = deepcopy(val)
                        return []
                else:
                    ls = [
                        1 + idx + self.n_outs
                        for idx in range(self.n_outs)
                        if not compute_map[inputs_false_branch[idx]][0]
                    ]
                    if len(ls) > 0:
                        return ls
                    else:
                        # strict=False because we are in a hot loop
                        for out, f in zip(outputs, inputs_false_branch, strict=False):
                            compute_map[out][0] = 1
                            # can't view both outputs unless destroyhandler
                            # improves
                            # Work around broken numpy deepcopy
                            val = storage_map[f][0]
                            if isinstance(val, np.ndarray | np.memmap):
                                storage_map[out][0] = val.copy()
                            else:
                                storage_map[out][0] = deepcopy(val)
                        return []

        thunk.lazy = True
        thunk.inputs = [storage_map[v] for v in node.inputs]
        thunk.outputs = [storage_map[v] for v in node.outputs]
        return thunk


def ifelse(
    condition: "TensorLike",
    then_branch: Any | Sequence[Any],
    else_branch: Any | Sequence[Any],
    name: str | None = None,
) -> Variable | Sequence[Variable]:
    """Construct a graph for an ``if`` statement.

    Parameters
    ----------
    condition
        `condition` should be a tensor scalar representing the condition.
        If it evaluates to ``0`` it corresponds to ``False``, anything else
        stands for ``True``.

    then_branch
        A single variable or a list of variables that the
        function should return as the output if `condition` evaluates to
        true. The number of variables should match those in the
        `else_branch`, as well as the dtypes and numbers of dimensions of each
        tensor.

    else_branch
        A single variable or a list of variables that the function should
        return as the output if `condition` evaluates to false. The number of
        variables should match those in `then_branch`, as well as the dtypes
        and numbers of dimensions of each tensor.

    Returns
    -------
        A sequence of variables or a single variable, depending on the
        nature of `then_branch` and `else_branch`.  More exactly, if
        `then_branch` and `else_branch` is are single variables, then
        the return variable will also be a single variable; otherwise, it will
        be a sequence. The value returned correspond to either the values in
        the `then_branch` or in the `else_branch` depending on the value of
        `condition`.
    """

    rval_type = None
    if isinstance(then_branch, list | tuple):
        rval_type = type(then_branch)
    else:
        then_branch = [then_branch]

    if not isinstance(else_branch, list | tuple):
        else_branch = [else_branch]

    if len(then_branch) != len(else_branch):
        raise ValueError(
            "The number of values on the `then` branch "
            "must match the `else` branch: got "
            f"{len(then_branch)} for `then`, and "
            f"{len(else_branch)} for `else`."
        )

    new_ifelse = IfElse(n_outs=len(then_branch), as_view=False, name=name)

    ins = [condition, *then_branch, *else_branch]
    rval = new_ifelse(*ins, return_list=True)

    if rval_type is None:
        return rval[0]
    elif rval_type is list:
        return list(rval)
    else:
        return tuple(rval)


@node_rewriter([IfElse])
def cond_make_inplace(fgraph, node):
    op = node.op
    if (
        isinstance(op, IfElse)
        and not op.as_view
        and
        # For big graph, do not make inplace scalar to speed up
        # optimization.
        (
            len(fgraph.apply_nodes) < 500
            or not all(getattr(o.type, "ndim", -1) == 0 for o in node.outputs)
        )
    ):
        return IfElse(n_outs=op.n_outs, as_view=True, name=op.name)(
            *node.inputs, return_list=True
        )
    return False


optdb.register(
    "cond_make_inplace",
    in2out(cond_make_inplace, ignore_newtrees=True),
    "fast_run",
    "inplace",
    position=95,
)

# XXX: Optimizations commented pending further debugging (certain optimizations
# make computation less lazy than it should be currently).
#
# ifelse_equilibrium = graph.rewriting.db.EquilibriumDB()
# ifelse_seqopt = graph.rewriting.db.SequenceDB()
# ifelse_equilibrium.register('seq_ifelse', ifelse_seqopt, 'fast_run',
#                             'ifelse')
""" Comments:
I've wrote this comments to explain how the optimization of ifelse function
(for future developers that need to parse this part of code. Please try to
keep this comments in sync with whatever changes you add to the code.

ifelse optimization are registered before canonicalize !

The optimizations are called in sequence as follows:
    * equilibrium shell (runs until no change):
        * ifelse_lift
        * ifelse_merge_ifs
        * ifelse_merge_nodes
        * ifelse_remove_identical_inside
        * ifelse_sameCondTrue_inside
        * ifelse_sameCondFalse_inside
    * merge_nodes_1
    * ifelse_sameCondTrue
    * ifelse_sameCondFalse
    * ifelse_removeIdentical

where, each of the optimization do the following things:
    `ifelse_lift` (def cond_lift_single_if):

"""
# optdb.register('ifelse_equilibriumOpt', ifelse_equilibrium, 'fast_run',
#                'ifelse', position=.5)


acceptable_ops = (
    Shape,
    SpecifyShape,
    Reshape,
    pt.math.Dot,
    pt.math.Max,
    pt.math.Argmax,
    pt.subtensor.Subtensor,
    pt.subtensor.IncSubtensor,
    pt.basic.Alloc,
    pt.elemwise.Elemwise,
    pt.elemwise.DimShuffle,
    pt.blockwise.Blockwise,
)


@node_rewriter(acceptable_ops)
def ifelse_lift_single_if_through_acceptable_ops(fgraph, main_node):
    """This optimization lifts up certain ifelse instances.

        op(ifelse(c, x, y)) -> ifelse(c, op(x), op(y))

    if `op` is in the `acceptable_ops` list, and there is no other if as
    input to that specific `op`, and the if has no other clients !?
    """
    if not (isinstance(main_node.op, acceptable_ops)):
        return False
    all_inp_nodes = set()
    for inp in main_node.inputs:
        all_inp_nodes.add(inp.owner)
    ifnodes = [x for x in list(all_inp_nodes) if x and isinstance(x.op, IfElse)]
    # if we have multiple ifs as inputs .. it all becomes quite complicated
    # :)
    if len(ifnodes) != 1:
        return False
    node = ifnodes[0]
    op = node.op

    aes = node.inputs[1:][: op.n_outs]
    fs = node.inputs[1:][op.n_outs :]

    # outs = main_node.outputs
    mop = main_node.op
    true_ins = []
    false_ins = []

    for x in main_node.inputs:
        if x in node.outputs:
            idx = node.outputs.index(x)
            true_ins.append(aes[idx])
            false_ins.append(fs[idx])
        else:
            true_ins.append(x)
            false_ins.append(x)
    true_eval = mop(*true_ins, return_list=True)
    false_eval = mop(*false_ins, return_list=True)
    # true_eval  = clone_replace(outs, replace = dict(zip(node.outputs, aes)))
    # false_eval = clone_replace(outs, replace = dict(zip(node.outputs, fs)))

    nw_outs = ifelse(node.inputs[0], true_eval, false_eval, return_list=True)
    return nw_outs


@node_rewriter([IfElse])
def cond_merge_ifs_true(fgraph, node):
    op = node.op
    if not isinstance(op, IfElse):
        return False
    t_ins = node.inputs[1:][: op.n_outs]

    replace = {}
    for idx, tval in enumerate(t_ins):
        if (
            tval.owner
            and isinstance(tval.owner.op, IfElse)
            and tval.owner.inputs[0] == node.inputs[0]
        ):
            ins_op = tval.owner.op
            ins_t = tval.owner.inputs[1:][: ins_op.n_outs]
            replace[idx + 1] = ins_t[tval.owner.outputs.index(tval)]

    if len(replace) == 0:
        return False

    old_ins = list(node.inputs)
    for pos, var in replace.items():
        old_ins[pos] = var
    return op(*old_ins, return_list=True)


@node_rewriter([IfElse])
def cond_merge_ifs_false(fgraph, node):
    op = node.op
    if not isinstance(op, IfElse):
        return False
    f_ins = node.inputs[1:][op.n_outs :]

    replace = {}
    for idx, fval in enumerate(f_ins):
        if (
            fval.owner
            and isinstance(fval.owner.op, IfElse)
            and fval.owner.inputs[0] == node.inputs[0]
        ):
            ins_op = fval.owner.op
            ins_t = fval.owner.inputs[1:][ins_op.n_outs :]
            replace[idx + 1 + op.n_outs] = ins_t[fval.owner.outputs.index(fval)]

    if len(replace) == 0:
        return False

    old_ins = list(node.inputs)
    for pos, var in replace.items():
        old_ins[pos] = var
    return op(*old_ins, return_list=True)


class CondMerge(GraphRewriter):
    """Graph Optimizer that merges different cond ops"""

    def add_requirements(self, fgraph):
        from pytensor.graph.features import ReplaceValidate

        fgraph.add_feature(ReplaceValidate())

    def apply(self, fgraph):
        nodelist = list(fgraph.toposort())
        cond_nodes = [s for s in nodelist if isinstance(s.op, IfElse)]
        if len(cond_nodes) < 2:
            return False
        merging_node = cond_nodes[0]
        for proposal in cond_nodes[1:]:
            if proposal.inputs[0] == merging_node.inputs[0] and not apply_depends_on(
                proposal, merging_node
            ):
                # Create a list of replacements for proposal
                mn_ts = merging_node.inputs[1:][: merging_node.op.n_outs]
                mn_fs = merging_node.inputs[1:][merging_node.op.n_outs :]
                pl_ts = proposal.inputs[1:][: proposal.op.n_outs]
                pl_fs = proposal.inputs[1:][proposal.op.n_outs :]
                new_ins = [merging_node.inputs[0], *mn_ts, *pl_ts, *mn_fs, *pl_fs]
                mn_name = "?"
                if merging_node.op.name:
                    mn_name = merging_node.op.name
                pl_name = "?"
                # mn_n_ts = len(mn_ts)
                # mn_n_fs = len(mn_fs)
                if proposal.op.name:
                    pl_name = proposal.op.name
                new_ifelse = IfElse(
                    n_outs=len(mn_ts + pl_ts),
                    as_view=False,
                    name=mn_name + "&" + pl_name,
                )
                new_outs = new_ifelse(*new_ins, return_list=True)
                new_outs = [clone_replace(x) for x in new_outs]
                old_outs = []
                if not isinstance(merging_node.outputs, list | tuple):
                    old_outs += [merging_node.outputs]
                else:
                    old_outs += merging_node.outputs
                if not isinstance(proposal.outputs, list | tuple):
                    old_outs += [proposal.outputs]
                else:
                    old_outs += proposal.outputs
                pairs = list(zip(old_outs, new_outs, strict=True))
                fgraph.replace_all_validate(pairs, reason="cond_merge")


@node_rewriter([IfElse])
def cond_remove_identical(fgraph, node):
    op = node.op

    if not isinstance(op, IfElse):
        return False
    aes = node.inputs[1:][: op.n_outs]
    fs = node.inputs[1:][op.n_outs :]

    # sync outs
    out_map = {}
    for idx in range(len(node.outputs)):
        if idx not in out_map:
            for jdx in range(idx + 1, len(node.outputs)):
                if aes[idx] == aes[jdx] and fs[idx] == fs[jdx] and jdx not in out_map:
                    out_map[jdx] = idx

    if len(out_map) == 0:
        return False

    nw_ts = []
    nw_fs = []
    inv_map = {}
    pos = 0
    for idx in range(len(node.outputs)):
        if idx not in out_map:
            inv_map[idx] = pos
            pos = pos + 1
            nw_ts.append(aes[idx])
            nw_fs.append(fs[idx])

    new_ifelse = IfElse(n_outs=len(nw_ts), as_view=op.as_view, name=op.name)

    new_ins = [node.inputs[0], *nw_ts, *nw_fs]
    new_outs = new_ifelse(*new_ins, return_list=True)

    rval = []
    for idx in range(len(node.outputs)):
        if idx in out_map:
            rval += [new_outs[inv_map[out_map[idx]]]]
        else:
            rval += [new_outs[inv_map[idx]]]

    return rval


@node_rewriter([IfElse])
def cond_merge_random_op(fgraph, main_node):
    if isinstance(main_node.op, IfElse):
        return False

    all_inp_nodes = set()
    for inp in main_node.inputs:
        all_inp_nodes.add(inp.owner)
    cond_nodes = [x for x in list(all_inp_nodes) if x and isinstance(x.op, IfElse)]

    if len(cond_nodes) < 2:
        return False

    merging_node = cond_nodes[0]
    for proposal in cond_nodes[1:]:
        if (
            proposal.inputs[0] == merging_node.inputs[0]
            and not apply_depends_on(proposal, merging_node)
            and not apply_depends_on(merging_node, proposal)
        ):
            # Create a list of replacements for proposal
            mn_ts = merging_node.inputs[1:][: merging_node.op.n_outs]
            mn_fs = merging_node.inputs[1:][merging_node.op.n_outs :]
            pl_ts = proposal.inputs[1:][: proposal.op.n_outs]
            pl_fs = proposal.inputs[1:][proposal.op.n_outs :]
            new_ins = [merging_node.inputs[0], *mn_ts, *pl_ts, *mn_fs, *pl_fs]
            mn_name = "?"
            if merging_node.op.name:
                mn_name = merging_node.op.name
            pl_name = "?"
            # mn_n_ts = len(mn_ts)
            # mn_n_fs = len(mn_fs)
            if proposal.op.name:
                pl_name = proposal.op.name
            new_ifelse = IfElse(
                n_outs=len(mn_ts + pl_ts),
                as_view=False,
                name=mn_name + "&" + pl_name,
            )
            new_outs = new_ifelse(*new_ins, return_list=True)
            old_outs = []
            if not isinstance(merging_node.outputs, list | tuple):
                old_outs += [merging_node.outputs]
            else:
                old_outs += merging_node.outputs
            if not isinstance(proposal.outputs, list | tuple):
                old_outs += [proposal.outputs]
            else:
                old_outs += proposal.outputs
            pairs = list(zip(old_outs, new_outs, strict=True))
            main_outs = clone_replace(main_node.outputs, replace=pairs)
            return main_outs


# XXX: Optimizations commented pending further debugging (certain optimizations
# make computation less lazy than it should be currently).
#
# pushout_equilibrium = graph.rewriting.db.EquilibriumDB()
#
# XXX: This optimization doesn't seem to exist anymore?
# pushout_equilibrium.register("cond_lift_single_if",
#                              in2out(cond_lift_single_if,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register("cond_merge_random_op",
#                              in2out(cond_merge_random_op,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
#
# pushout_equilibrium.register("ifelse_merge",
#                              graph.opt.MergeOptimizer(skip_const_merge=False),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register("ifelse_remove_identical_inside",
#                              in2out(cond_remove_identical,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register('ifelse_sameCondTrue_inside',
#                              in2out(cond_merge_ifs_true,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# pushout_equilibrium.register('ifelse_sameCondFalse_inside',
#                              in2out(cond_merge_ifs_false,
#                                         ignore_newtrees=True),
#                              'fast_run', 'ifelse')
#
# ifelse_seqopt.register('ifelse_condPushOut_equilibrium',
#                        pushout_equilibrium,
#                        'fast_run', 'ifelse', position=1)
#
# ifelse_seqopt.register('merge_nodes_1',
#                        graph.opt.MergeOptimizer(skip_const_merge=False),
#                        'fast_run', 'ifelse', position=2)
#
#
# ifelse_seqopt.register('ifelse_sameCondTrue',
#                        in2out(cond_merge_ifs_true,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=3)
#
#
# ifelse_seqopt.register('ifelse_sameCondFalse',
#                        in2out(cond_merge_ifs_false,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=4)
#
#
# ifelse_seqopt.register('ifelse_removeIdenetical',
#                        in2out(cond_remove_identical,
#                                   ignore_newtrees=True),
#                        'fast_run', 'ifelse', position=7)
