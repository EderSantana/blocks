"""Annotated computation graph management."""
import logging
from collections import OrderedDict
from itertools import chain

import numpy
import theano
from theano import Variable
from theano.gof import graph
from theano.gof.sched import make_dependence_cmp, sort_apply_nodes
from theano.sandbox.rng_mrg import MRG_RandomStreams

from blocks import config
from blocks.roles import add_role, AUXILIARY
from blocks.utils import (is_graph_input, is_shared_variable, dict_union,
                          shared_floatx_zeros, shared_like)

logger = logging.getLogger(__name__)
dependence = make_dependence_cmp()


class ComputationGraph(object):
    """Encapsulates a managed Theano computation graph.

    This implies that it not only contains the variables required to
    compute the given outputs, but also all the auxiliary variables and
    updates that were attached to these variables through the annotation
    system.

    All variables are presented in topologically sorted order according to
    the apply nodes that they are an input to.

    Parameters
    ----------
    outputs : Theano variable or list of Theano variables
        The output(s) of the computation graph.

    Attributes
    ----------
    inputs : list of Theano variables
        The inputs of the computation graph. This does not include shared
        variables and constants.
    shared_variables : list of Theano shared variables
        All the shared variables in the graph.
    outputs : list of Theano variables
        The outputs of the computations graph (as passed to the
        constructor).
    auxiliary_variables : list of Theano variables
        All variables which have the :attr:`Variable.AUXILIARY` role.
    intermediary_variables : list of Theano variables
        Any variable that is not part of :attr:`inputs` or :attr:`outputs`.
    variables : list of Theano variables
        All variables (including auxiliary) in the managed graph.
    updates : list of (Theano variable, Theano expression) pairs
        All the updates found attached to the annotations.

    """
    def __init__(self, outputs):
        if isinstance(outputs, Variable):
            outputs = [outputs]
        self.outputs = outputs
        self._get_variables()

    @property
    def inputs(self):
        """Inputs to the graph, excluding constants and shared variables."""
        return [var for var in self.variables if is_graph_input(var)]

    @property
    def intermediary_variables(self):
        return [var for var in self.variables if
                var not in self.inputs and
                var not in self.outputs]

    @property
    def shared_variables(self):
        return [var for var in self.variables if is_shared_variable(var)]

    @property
    def auxiliary_variables(self):
        return [var for var in self.variables if hasattr(var.tag, 'roles') and
                AUXILIARY in var.tag.roles]

    def _get_variables(self):
        """Collect variables, updates and auxiliary variables."""
        updates = OrderedDict()

        # Sort apply nodes topologically, get variables and remove duplicates
        inputs = graph.inputs(self.outputs)
        sorted_apply_nodes = sort_apply_nodes([inputs], self.outputs,
                                              [dependence])
        seen = set()
        main_vars = [var for var in list(chain(
            *[apply_node.inputs for apply_node in sorted_apply_nodes]))
            if not (var in seen or seen.add(var))] + self.outputs

        # While preserving order add auxiliary variables, and collect updates
        seen = set()
        seen_avs = set(main_vars)  # Intermediate variables could be auxiliary
        variables = []
        for var in main_vars:
            variables.append(var)
            for annotation in getattr(var.tag, 'annotations', []):
                if annotation not in seen:
                    seen.add(annotation)
                    new_avs = [av for av in annotation.auxiliary_variables
                               if not (av in seen_avs or seen_avs.add(av))]
                    variables.extend(new_avs)
                    updates = dict_union(updates, annotation.updates)

        self.variables = variables
        self.updates = updates

    def dict_of_inputs(self):
        """Return a mapping from an input name to the input."""
        return {var.name: var for var in self.inputs}

    def replace(self, replacements):
        """Replace certain variables in the computation graph.

        Parameters
        ----------
        replacements : dict
            The mapping from variables to be replaced to the corresponding
            substitutes.

        """
        return ComputationGraph(theano.clone(self.outputs,
                                             replace=replacements))

    def get_theano_function(self, additional_updates=None):
        """Create Theano function from the graph contained."""
        updates = self.updates
        if additional_updates:
            updates = dict_union(updates, OrderedDict(additional_updates))
        return theano.function(self.inputs, self.outputs, updates=updates)

    def get_snapshot(self, data):
        """Evaluate all role-carrying Theano variables on given data.

        Parameters
        ----------
        data : dict of (data source, data) pairs
            Data for input variables. The sources should match with the
            names of the input variables.

        Returns
        -------
        Dictionary of (variable, variable value on given data) pairs.

        """
        role_variables = [var for var in self.variables
                          if hasattr(var.tag, "roles")]
        value_holders = [shared_like(var) for var in role_variables]
        function = self.get_theano_function(zip(value_holders, role_variables))
        function(*(data[input_.name] for input_ in self.inputs))
        return OrderedDict([(var, value_holder.get_value(borrow=True))
                            for var, value_holder in zip(role_variables,
                                                         value_holders)])


def add_annotation(var, annotation):
    annotations = getattr(var.tag, 'annotations', [])
    if any(old_annotation.__class__ == annotation.__class__
           for old_annotation in annotations):
        raise ValueError
    else:
        var.tag.annotations = annotations + [annotation]


class Annotation(object):
    """Annotations on Theano variables in a graph.

    In Blocks annotations are automatically attached to variables created
    using bricks. One form of annotation is that many variables are
    assigned a role (see :class:`VariableRole`). A second form of
    annotation comes in the form of attaching a :class:`Annotation`
    instance to the variable's ``tag`` attribute, with auxiliary variables
    and/or updates.

    For example, we might be interested in the mean activation of certain
    application of a :class:`Linear` brick. The variable representing the
    mean activation is attached as an auxiliary variable to the annotations
    of the input and output variables of this brick. Using the
    :class:`ComputationGraph` class (the
    :meth:`ComputationGraph.get_variables` method in particular) we can
    retrieve these Theano variables to pass on to the monitor, use as a
    regularizer, etc.

    In most cases, annotations are added on a brick level (e.g. each brick
    will assign the weight norm of its weights as an auxiliary value) or on
    an application level (e.g. each time a brick is applied, its mean
    activation will become an auxiliary variable). However, you can also
    add annotations manually, by setting the ``annotation`` value of a
    variable's ``tag`` field.

    Examples
    --------
    >>> from theano import tensor
    >>> x = tensor.vector()
    >>> annotation = Annotation()
    >>> annotation.add_auxiliary_variable(x + 1, name='x_plus_1')
    >>> add_annotation(x, annotation)
    >>> y = x ** 2
    >>> from blocks.graph import ComputationGraph
    >>> cg = ComputationGraph([y])
    >>> cg.auxiliary_variables
    [x_plus_1]

    """
    def __init__(self):
        self.auxiliary_variables = []
        self.updates = OrderedDict()

    def add_auxiliary_variable(self, expression, roles=None, name=None):
        """Attach an auxiliary variable to the graph.

        Auxiliary variables are Theano variables that are not part of a
        brick's output, but can be useful nonetheless e.g. as a regularizer
        or to monitor during training progress.

        Parameters
        ----------
        expression : Theano variable
            The expression of the variable you want to add.
        roles : list of :class:`VariableRole` instances, optional
            The roles of this variable. The :const:`AUXILIARY`
            role will automatically be added. Other options are
            :const:`COST`, :const:`WEIGHTS`, etc.
        name : str, optional
            The name of the expression; overrides the name of the variable
            if it already has one.

        Examples
        --------
        >>> from blocks.bricks.base import application, Brick
        >>> from blocks.roles import COST
        >>> from blocks.utils import shared_floatx_zeros
        >>> class Foo(Brick):
        ...     def _allocate(self):
        ...         W = shared_floatx_zeros((10, 10))
        ...         self.add_auxiliary_variable(W.mean(), name='mean_W')
        ...     @application
        ...     def apply(self, x, application_call):
        ...         application_call.add_auxiliary_variable(
        ...             x - 1, name='x_minus_1')
        ...         application_call.add_auxiliary_variable(
        ...             x.mean(), roles=[COST], name='mean_x')
        ...         return x + 1
        >>> from theano import tensor
        >>> x = tensor.vector()
        >>> y = Foo().apply(x)
        >>> from blocks.filter import VariableFilter
        >>> cg = ComputationGraph([y])
        >>> var_filter = VariableFilter(roles=[AUXILIARY])
        >>> var_filter(cg.variables) # doctest: +SKIP
        {x_minus_1, mean_W, mean_x}
        >>> var_filter = VariableFilter(roles=[COST])
        >>> var_filter(cg.variables) # doctest: +SKIP
        {mean_x}

        """
        add_annotation(expression, self)
        if name is not None:
            expression.name = name
            expression.tag.name = name
        add_role(expression, AUXILIARY)
        if roles is not None:
            for role in roles:
                add_role(expression, role)
        self.auxiliary_variables.append(expression)


def apply_noise(graph, variables, level, seed=None):
    """Add Gaussian noise to certain variable of a computation graph.

    Parameters
    ----------
    graph : instance of :class:`ComputationGraph`
        The computation graph.
    varibles : Theano variables
        Variables to add noise to.
    level : float
        Noise level.
    rng : Theano random stream, optional
        The random stream to use. By default an RNG with seed equal to 1 is
        used.

    """
    if not seed:
        seed = config.default_seed
    rng = MRG_RandomStreams(seed)
    replace = {}
    for variable in variables:
        replace[variable] = (variable +
                             rng.normal(variable.shape, std=level))
    return graph.replace(replace)


def collect_parameters(graph, params):
    """Replace parameters with a single shared variable.

    This can be useful if you need to calculate the full Hessian of a
    computational graph. It replaces parameters with slices of a single
    large vectors like

    >>> from blocks.utils import shared_floatx
    >>> W1 = shared_floatx(numpy.random.rand(10, 10))
    >>> W2 = shared_floatx(numpy.random.rand(10, 10))
    >>> all_params = shared_floatx(numpy.concatenate(
    ...     [W1.get_value().flatten(), W2.get_value().flatten()]))
    >>> W1 = all_params[:W1.size]
    >>> W2 = all_params[W1.size:]

    Parameters
    ----------
    graph : :class:`ComputationGraph` instance
        The managed Theano graph in which to collect parameters.
    params : list of Theano shared variables
        The parameters whose values should be collected.

    Returns
    -------
    ComputationGraph instance
        A new Theano graph which has all the given parameters collected
        into a single large shared variable.

    Notes
    -----
    Note that this replacement makes the training of the model
    significantly slower because of the large amount of Theano's
    ``set_subtensor`` calls needed to train the model.

    Examples
    --------
    >>> from blocks.bricks import MLP, Sigmoid
    >>> from blocks.bricks.cost import SquaredError
    >>> from theano import tensor
    >>> x = tensor.matrix()
    >>> mlp = MLP(activations=[Sigmoid(), Sigmoid()], dims=[784, 100, 784])
    >>> cost = SquaredError().apply(x, mlp.apply(x))
    >>> cg = ComputationGraph(cost)
    >>> new_cg = collect_parameters(cg, cg.shared_variables)

    The new graph only has a single shared variable.

    >>> new_cg.shared_variables
    [collected_params]

    The bricks' variables have been replaced with reshaped segments of this
    single shared variable.

    >>> from blocks.filter import VariableFilter
    >>> var_filter = VariableFilter(roles=[PARAMETER])
    >>> var_filter(new_cg.variables) # doctest: +SKIP
    [Reshape{1}.0, Reshape{1}.0, Reshape{2}.0, Reshape{2}.0]

    """
    param_values, param_sizes, param_shapes = [], [], []
    for param in params:
        param_values.append(param.get_value(borrow=True))
        param_sizes.append(param_values[-1].size)
        param_shapes.append(param_values[-1].shape)

    new_params = shared_floatx_zeros(sum(param_sizes))
    new_params.name = 'collected_params'

    replacements = {}
    for param, shape, i, j in zip(params, param_shapes,
                                  numpy.cumsum([0] + param_sizes[:-1]),
                                  numpy.cumsum(param_sizes)):
        new_param = new_params[i:j].reshape(shape)
        new_param.tag = param.tag
        replacements[param] = new_param
    return graph.replace(replacements)
