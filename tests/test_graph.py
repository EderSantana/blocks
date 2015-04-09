import numpy
import theano
from numpy.testing import assert_allclose
from theano import tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams

from blocks.bricks import MLP, Identity
from blocks.graph import (apply_noise, apply_batch_normalization,
                          ComputationGraph)
from blocks.initialization import Constant
from tests.bricks.test_bricks import TestBrick

floatX = theano.config.floatX


def test_application_graph_auxiliary_vars():
    X = tensor.matrix('X')
    brick = TestBrick(0)
    Y = brick.access_application_call(X)
    graph = ComputationGraph(outputs=[Y])
    test_val_found = False
    for var in graph.variables:
        if var.name == 'test_val':
            test_val_found = True
            break
    assert test_val_found


def test_computation_graph():
    x = tensor.matrix('x')
    y = tensor.matrix('y')
    z = x + y
    z.name = 'z'
    a = z.copy()
    a.name = 'a'
    b = z.copy()
    b.name = 'b'
    r = tensor.matrix('r')

    cg = ComputationGraph([a, b])
    assert set(cg.inputs) == {x, y}
    assert set(cg.outputs) == {a, b}
    assert set(cg.variables) == {x, y, z, a, b}
    assert cg.variables[2] is z
    assert ComputationGraph(a).inputs == cg.inputs

    cg2 = cg.replace({z: r})
    assert set(cg2.inputs) == {r}
    assert set([v.name for v in cg2.outputs]) == {'a', 'b'}

    W = theano.shared(numpy.zeros((3, 3), dtype=floatX))
    cg3 = ComputationGraph([z + W])
    assert set(cg3.shared_variables) == {W}

    cg4 = ComputationGraph([W])
    assert cg4.variables == [W]

    w1 = W ** 2
    cg5 = ComputationGraph([w1])
    assert W in cg5.variables
    assert w1 in cg5.variables

    # Test scan
    s, _ = theano.scan(lambda inp, accum: accum + inp,
                       sequences=x,
                       outputs_info=tensor.zeros_like(x[0]))
    scan = s.owner.inputs[0].owner.op
    cg6 = ComputationGraph(s)
    assert cg6.scans == [scan]
    assert all(v in cg6.scan_variables for v in scan.inputs + scan.outputs)


def test_computation_graph_variable_duplicate():
    # Test if ComputationGraph.variables contains duplicates if some outputs
    # are part of the computation graph
    x, y = tensor.matrix('x'), tensor.matrix('y')
    w = x + y
    z = tensor.exp(w)

    cg = ComputationGraph([z, w])
    assert len(set(cg.variables)) == len(cg.variables)


def test_replace():
    # Test if replace works with outputs
    x = tensor.scalar()
    y = x + 1
    cg = ComputationGraph([y])
    doubled_cg = cg.replace([(y, 2 * y)])
    out_val = doubled_cg.outputs[0].eval({x: 2})
    assert out_val == 6.0


def test_replace_multiple_inputs():
    # Test if replace works on variables that are input to multiple nodes
    x = tensor.scalar('x')
    y = 2 * x
    z = x + 1

    cg = ComputationGraph([y, z]).replace({x: 0.5 * x})
    assert_allclose(cg.outputs[0].eval({x: 1.0}), 1.0)
    assert_allclose(cg.outputs[1].eval({x: 1.0}), 1.5)


def test_apply_noise():
    x = tensor.scalar()
    y = tensor.scalar()
    z = x + y

    cg = ComputationGraph([z])
    noised_cg = apply_noise(cg, [y], 1, 1)
    assert_allclose(
        noised_cg.outputs[0].eval({x: 1., y: 1.}),
        2 + MRG_RandomStreams(1).normal(tuple()).eval())


def test_snapshot():
    x = tensor.matrix('x')
    linear = MLP([Identity(), Identity()], [10, 10, 10],
                 weights_init=Constant(1), biases_init=Constant(2))
    linear.initialize()
    y = linear.apply(x)
    cg = ComputationGraph(y)
    snapshot = cg.get_snapshot(dict(x=numpy.zeros((1, 10), dtype=floatX)))
    assert len(snapshot) == 14


def test_apply_batch_normalization():
    x = tensor.matrix()
    y = 2 * x

    cg = ComputationGraph([y])
    cg_bn = apply_batch_normalization(
        cg, [x], [numpy.array([2, 3]).astype(floatX)],
        [numpy.array([-1, -2]).astype(floatX)])
    assert_allclose(
        cg_bn.outputs[0].eval(
            {x: numpy.arange(4).reshape((2, 2)).astype(floatX)}),
        [[-6., -10.], [2., 2.]],
        atol=1e-5)


def test_apply_batch_normalization_use_population():
    x = tensor.matrix()
    y = 2 * x

    cg = ComputationGraph([y])
    cg_bn = apply_batch_normalization(
        cg, [x], [numpy.array([2, 3]).astype(floatX)],
        [numpy.array([-1, -2]).astype(floatX)], use_population={x: True})
    assert_allclose(
        cg_bn.outputs[0].eval(
            {x: numpy.arange(4).reshape((2, 2)).astype(floatX)}),
        [[-2., 2.], [6., 14.]],
        atol=1e-5)


def test_apply_batch_normalization_conv():
    x = tensor.tensor4()
    y = 2 * x

    cg = ComputationGraph([y])
    cg_bn = apply_batch_normalization(
        cg, [x], [numpy.array([2, 3]).astype(floatX)],
        [numpy.array([-1, -2]).astype(floatX)], axis=[0, 2, 3])
    assert_allclose(
        cg_bn.outputs[0].eval(
            {x: numpy.arange(16).reshape((2, 2, 2, 2)).astype(floatX)}),
        [[[[-7.29697754, -6.33389071],
           [-5.37080389, -4.40771706]],
          [[-11.9454663, -10.50083607],
           [-9.05620583, -7.61157559]]],
         [[[0.40771706, 1.37080389],
           [2.33389071, 3.29697754]],
          [[-0.38842441, 1.05620583],
           [2.50083607, 3.9454663]]]],
        atol=1e-5)
