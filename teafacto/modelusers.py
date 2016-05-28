import theano

from teafacto.core.base import Input


class ModelUser(object):
    def __init__(self, model, **kw):
        super(ModelUser, self).__init__(**kw)
        self.model = model
        self.f = None


class RecPredictor(ModelUser):
    def __init__(self, model, *buildargs, **kw):
        super(RecPredictor, self).__init__(model, **kw)
        self.statevals = None
        self.buildargs = buildargs

    def build(self, inps):  # data: (batsize, ...)
        batsize = inps[0].shape[0]
        inits = self.model.get_init_info(*(list(self.buildargs)+[batsize]))
        inpvars = [Input(ndim=inp.ndim, dtype=inp.dtype) for inp in inps]
        statevars = [Input(ndim=x.d.ndim, dtype=x.d.dtype) for x in inits]
        allinpvars = inpvars + statevars
        out = self.model.rec(*(inpvars+statevars))
        alloutvars = out
        self.f = theano.function(inputs=[x.d for x in allinpvars], outputs=[x.d for x in alloutvars])
        self.statevals = [x.d.eval() for x in inits]

    def feed(self, *inps):  # inps: (batsize, ...)
        if self.f is None:      # build
            self.build(inps)
        inpvals = list(inps) + self.statevals
        outpvals = self.f(*inpvals)
        self.statevals = outpvals[1:]
        return outpvals[0]