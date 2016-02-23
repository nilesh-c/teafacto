import theano
from theano import tensor as T
from theano.tensor.var import _tensor_py_operators

from teafacto.blocks.datafeed import DataFeed
from teafacto.blocks.trainer import *

from teafacto.core.init import *
from lasagne.init import *
from lasagne.updates import norm_constraint


class TWrapper(type):
    def __getattr__(cls, item):
        top = getattr(T, item)
        return wrapf(top)

def wrapf(attr, addparent=None):
    if hasattr(attr, "__call__"): # real function
        innerwrap = lambda *args, **kwargs: fwrap(attr, addparent, *args, **kwargs)
    else:
        innerwrap = lambda: pwrap(attr)
    return innerwrap

def fwrap(attr, addparent, *args, **kwargs):
    params = [x for x in args if isinstance(x, Parameter)]
    kwparams = [x for x in kwargs.values() if isinstance(x, Parameter)]
    ret = wrap(lambda *args, **kwargs: attr(*args, **kwargs), *(params+kwparams))(*args, **kwargs)
    if addparent is not None:
        ret.add_parent(addparent)
    return ret

def pwrap(attr):
    return attr


class tensorops:
    __metaclass__ = TWrapper

class TensorWrapper(type):
    """Wrapper class that provides proxy access to an instance of some
       internal instance."""

    __ignore__ = "class mro new init setattr getattr getattribute"

    def __init__(cls, name, bases, dct):

        def make_proxy(name):
            def proxy(self, *args):
                attr = getattr(self.d, name)
                return wrapf(attr, addparent=self)
            return proxy

        type.__init__(cls, name, bases, dct)
        ignore = set("__%s__" % n for n in cls.__ignore__.split())
        for name in dir(_tensor_py_operators):
            if name.startswith("__"):
                if name not in ignore and name not in dct:
                    setattr(cls, name, property(make_proxy(name)))

class Parameter(object):
    '''
    A parameter wraps a shared variable and can optionally have a different learning rate and regularization multiplier
    '''
    def __init__(self, value, name=None, lrmul=1., regmul=1., shape=None):
        self.initializer = None
        if isinstance(value, theano.compile.sharedvalue.SharedVariable):
            self.value = value
            self.shape = value.get_value().shape
            self.initializer = lambda: value.get_values()
        elif isinstance(value, Initializer):
            self.shape = shape
            self.initializer = lambda: value.sample(shape)
            self.value = theano.shared(np.zeros(shape))
            self.reset()
        else:
            self.value = theano.shared(value)
            self.initializer = lambda: value
            self.shape = value.shape
        self.lrmul = lrmul
        self.regmul = regmul
        self.name = str(name) if name is not None else "auto:" + str(np.random.randint(0, 10000))
        self.constraints = []

    def reset(self):
        self.value.set_value(self.initializer())

    @property
    def d(self):
        return self.value

    def __repr__(self):
        return "param::%s=%s:%s-%.1f:%.1f" % (self.name, str(self.value.dtype), str(self.value.get_value().shape), self.lrmul, self.regmul)

    ############## VALUE CONSTRAINTS ############### --> applied in the order that the were added
    def clip(self, a, b):
        self.constraints.append(lambda x: T.clip(x, a, b))
        return self

    def normalize(self, axis=0, norm=2, epsilon=1e-7):
        self.constraints.append(lambda x: x/(x.norm(norm, axis=axis)+epsilon)) # TODO
        return self

    def norm_constraint(self, max_norm, norm_axes=None, epsilon=1e-7):
        self.constraints.append(lambda x: norm_constraint(x, max_norm=max_norm, norm_axes=norm_axes, epsilon=epsilon))
        return self

    def constraintf(self):
        cs = self.constraints
        def innerconstraintf(x):
            ret = x
            for cf in cs:
                ret = cf(ret)
            return ret
        return innerconstraintf



class param(object):
    def __init__(self, shape, lrmul=1., regmul=1.):
        self.shape = shape
        self.lrmul = lrmul
        self.regmul = regmul
        self.value = None

    def random(self, offset=0.5, scale=0.1):
        init = lambda: random(self.shape, offset, scale)
        ret = Parameter(init, self.lrmul, self.regmul)
        ret.initializer = init
        return ret

    ############## LASAGE INITS ################
    def _lasagne_init(self, initializer):
        return Parameter(initializer, self.lrmul, self.regmul, shape=self.shape)

    def uniform(self, range=0.01, std=None, mean=0.0):
        return self._lasagne_init(Uniform(range, std, mean))

    def normal(self, std=0.01, mean=0.0):
        return self._lasagne_init(Normal(std, mean))

    def glorotnormal(self, gain=1.0, c01b=False):
        return self._lasagne_init(GlorotNormal(gain, c01b))

    def glorotuniform(self, gain=1.0, c01b=False):
        return self._lasagne_init(GlorotUniform(gain, c01b))

    def henormal(self, gain=1.0, c01b=False):
        return self._lasagne_init(HeNormal(gain, c01b))

    def heuniform(self, gain=1.0, c01b=False):
        return self._lasagne_init(HeUniform(gain, c01b))

    def constant(self, val=0.0):
        return self._lasagne_init(Constant(val))

    def sparse(self, sparsity=0.1, std=0.01):
        return self._lasagne_init(Sparse(sparsity, std))

    def orthogonal(self, gain=1.0):
        return self._lasagne_init(Orthogonal(gain))


class Elem(object):    # carries output shape information
    def __init__(self, shape=None, name=None, **kw):
        super(Elem, self).__init__()
        self._shape = shape
        self._name = name

    @property
    def dshape(self): # returns declared shape
        return self._shape

    @property
    def allparams(self):
        acc = set()
        if hasattr(self, "params"):
            acc.update(set(self.params))
        for parent in self.getparents():
            acc.update(parent.allparams)
        return acc

    def getparents(self):
        raise NotImplementedError("use subclass")


class Var(Elem): # result of applying a block on theano variables
    __metaclass__ = TensorWrapper

    def __getattr__(self, item):
        return wrapf(getattr(self.tvar, item), addparent=self)

    def __init__(self, tvar, parent=None, **kw):
        super(Var, self).__init__(name=tvar.name, **kw)
        assert(isinstance(tvar, theano.Variable))
        self.tvar = tvar
        self.parents = [] # can only have one parent (a block)
        if parent is not None:
            self.add_parent(parent)

    def getparents(self):
        return self.parents

    def add_parent(self, p):
        self.parents.append(p)

    def eval(self, argdic):
        return self.d.eval(dict(map(lambda (x, y): (x.d, y), argdic.items())))

    @property
    def d(self):
        return self.tvar

    def __repr__(self):
        return "var::%s-%s:%s" % (self._name, self.tvar.dtype, str(self._shape))


class Input(Var): # generates feed + creates symbolic vars for input
    def __init__(self, ndim, dtype, name=None, **kw): # data source (numpy array)
        value = T.TensorType(dtype, (False,)*ndim)(name=name)
        super(Input, self).__init__(value, parent=None, **kw)
        self.ndim = ndim # store number of dimensions

    def dimswap(self, a, b):
        dims = range(self.ndim)
        dims[a] = b
        dims[b] = a
        ret = self.d.dimshuffle(*dims)
        return Var(ret, [self])


class Block(Elem): # block with parameters
    def __init__(self, **kw):
        super(Block, self).__init__(**kw)
        self.inputs = []
        self.parents = []
        self.params = []
        self._predictf = None

    def initinputs(self): # must override to be trainable
        return []

    def apply(self, *vars, **kwargs):
        trueargs = [x.d if hasattr(x, "d") else x for x in vars]
        truekwargs = dict(map(lambda (x, y): (x, y.d if hasattr(y, "d") else y), kwargs.items()))
        result = self._apply(*trueargs, **truekwargs)
        return Var(result, parent=self)

    # may override: -------------------------------------------------
    def predict(self, inputdata):
        if self._predictf is None:
            self.build()
            self._predictf = theano.function(outputs=self.output.d, inputs=[x.d for x in self.inputs])
        return self._predictf(dict(zip([x.d for x in self.inputs], inputdata)))

    def gettrainer(self, goldvar):
        return ModelTrainer(self, goldvar)

    # do not override ------------------------------------------------
    def getparents(self):
        return self.parents

    def add_params(self, params):
        for param in params:
            self.add_param(param)

    def add_param(self, p): # always returns a Parameter
        if isinstance(p, Parameter):
            p = p
        elif isinstance(p, theano.compile.sharedvalue.SharedVariable): # if shared var --> wrap in a param
            p = Parameter(p)
        elif isinstance(p, np.ndarray): # numpy array
            p = Parameter(param(p))
        elif isinstance(p, tuple): # try to decode as a list of (param, lrmul, regmul) entries --> wrap in a param
            assert(isinstance(p[0], theano.compile.sharedvalue.SharedVariable))
            lrmul = 1.
            regmul = 1.
            p = p[0]
            if len(p) > 1:
                lrmul = p[1]
            if len(p) > 2:
                regmul = p[2]
            p = Parameter(p, lrmul=lrmul, regmul=regmul)
        self.params.append(p)
        return p

    def __call__(self, *args, **kwargs):
        return self.wrapply(*args, **kwargs)

    def wrapply(self, *args, **kwargs):
        self.parents.extend([x for x in args if isinstance(x, Var)])
        self.parents.extend([x for x in kwargs.values() if isinstance(x, Var)])
        ret = self.apply(*args, **kwargs)
        ret.add_parent(self) # TODO: remove??
        return ret

    def build(self): # stores block inputs and block output
        self.inputs = self.initinputs()
        self.output = self.wrapply(*self.inputs)

    def train(self, inputdata, gold):
        # wrap data in datafeeds, generate gold var
        goldvar = Input(gold.ndim, gold.dtype, name="gold")
        trainer = self.gettrainer(goldvar.d)
        trainer.traindata = inputdata
        trainer.traingold = gold
        return trainer


class wrap(Block): # wraps a theano symbolic expression into a block
    def __init__(self, fun, *params, **kw):
        super(wrap, self).__init__(**kw)
        self.add_params(params)
        assert(hasattr(fun, "__call__"))
        self.opfun = fun

    def _apply(self, *tvars, **kwargs):
        return self.opfun(*tvars, **kwargs)


class FeedForward(Block): # feedforward
    def __init__(self, indim, dim, activation=None, **kw):
        super(FeedForward, self).__init__(**kw)
        self.W = self.add_param(random((indim, dim))).d
        self.b = self.add_param(random((dim, ))).d

    def _apply(self, tvar):
        return T.dot(tvar, self.W) + self.b


if __name__ == "__main__":
    '''x = Input(2, "int32", name="x")
    E = param((10, 10)).uniform()
    W = param((10, 10)).uniform()
    y = wrap(lambda x: E[x, :], E)(x)
    y = FeedForward(11, 12)(y)
    '''
    '''
    model = Model(y, [x])
    errors = model.train([xval], gval).cross_entropy().l2(0.001).sgd(lr) \
                  .cross_validate(5).cross_entropy.accuracy(y, g) \
                  .train()
    prediction = model.predict([xval])
    '''
    '''
    print y.allinputs
    print y.allparams
    print x
    '''
    x = Input(1, "float32")
    W = param((10, 10)).uniform()
    y = tensorops.dot(W, x)
    normparam = theano.shared(0)
    normparam = Parameter(normparam)
    xval = np.random.random((10,)).astype("float32")
    #print xval
    #print y.allparams
    #print y.eval({x: xval})
    z = (x + y).norm(2, axis=0)
    print z.eval({x: xval})
    print z.allparams