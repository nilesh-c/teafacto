from theano.tensor.extra_ops import to_one_hot

__author__ = 'denis'
import theano
from theano import tensor as T
import numpy as np, math
from tf import TFSGD
from utils import *
from math import ceil, floor
from datetime import datetime as dt
from IPython import embed
import sys, os, cPickle as pickle, inspect
from rnnkm import Saveable, Profileable, Predictor, Normalizable
from rnn import GRU, LSTM, RNUBase
from optimizers import SGD, RMSProp, AdaDelta, Optimizer

class SGDBase(object):
    def __init__(self, maxiter=50, lr=0.0001, numbats=100, wreg=0.00001, **kw):
        self.maxiter = maxiter
        self.currentiter = 0
        self.numbats = numbats
        self.wreg = wreg
        self.tnumbats = theano.shared(np.float32(self.numbats), name="numbats")
        self.twreg = theano.shared(np.float32(self.wreg), name="wreg")
        self._optimizer = SGD(lr)
        super(SGDBase, self).__init__(**kw)

    @property
    def printname(self):
        return self.__class__.__name__ + "+" + self._optimizer.__class__.__name__

    def __add__(self, other):
        if isinstance(other, Optimizer):
            self._optimizer = other
            other.onattach(self)
            return self
        else:
            raise Exception("unknown type of composition argument")

    def gettrainf(self, finps, fouts, cost):
        params = self.ownparams + self.depparams
        grads = T.grad(cost, wrt=params)
        updates = self.getupdates(params, grads)
        #showgraph(updates[0][1])
        return theano.function(inputs=finps,
                               outputs=fouts,
                               updates=updates,
                               profile=self._profiletheano)

    def getupdates(self, params, grads):
        return self._optimizer.getupdates(params, grads)

    def trainloop(self, trainf, validf=None, evalinter=1, normf=None, average_err=True):
        err = []
        stop = False
        self.currentiter = 1
        evalcount = evalinter
        #if normf:
        #    normf()
        while not stop:
            print("iter %d/%.0f" % (self.currentiter, float(self.maxiter)))
            start = dt.now()
            erre = trainf()
            if average_err:
                erre /= self.numbats
            if normf:
                normf()
            if self.currentiter == self.maxiter:
                stop = True
            self.currentiter += 1
            err.append(erre)
            print(erre)
            print("iter done in %f seconds" % (dt.now() - start).total_seconds())
            evalcount += 1
            if self._autosave:
                self.save()
        return err

    def getbatchloop(self, trainf, samplegen):
        '''
        returns the batch loop, loaded with the provided trainf training function and samplegen sample generator
        '''
        numbats = self.numbats

        def batchloop():
            c = 0
            prevperc = -1.
            maxc = numbats
            terr = 0.
            while c < maxc:
                #region Percentage counting
                perc = round(c*100./maxc)
                if perc > prevperc:
                    sys.stdout.write("iter progress %.0f" % perc + "% \r")
                    sys.stdout.flush()
                    prevperc = perc
                #endregion
                sampleinps = samplegen()
                terr += trainf(*sampleinps)[0]
                c += 1
            return terr
        return batchloop


class KMSM(SGDBase, Saveable, Profileable, Predictor, Normalizable):
    def __init__(self, vocabsize=10, negrate=None, margin=None, **kw):
        super(KMSM, self).__init__(**kw)
        self.vocabsize = vocabsize

    def train(self, trainX, labels, evalinter=10):
        self.batsize = int(ceil(trainX.shape[0]*1./self.numbats))
        self.tbatsize = theano.shared(np.int32(self.batsize))
        model = self.defmodel() # the last element is list of inputs, all others go to geterr()
        tErr = self.geterr(*model[:-1])
        tReg = self.getreg()
        #embed()
        tCost = tErr + tReg
        #showgraph(tCost)
        #embed() # tErr.eval({inps[0]: [0], inps[1]:[10], gold: [1]})

        trainf = self.gettrainf(model[-1], [tErr, tCost], tCost)
        err = self.trainloop(trainf=self.getbatchloop(trainf, self.getsamplegen(trainX, labels)),
                             evalinter=evalinter,
                             normf=self.getnormf())
        return err

    def defmodel(self):
        pathidxs = T.imatrix("pathidxs")
        zidx = T.ivector("zidx") # rhs corruption only
        scores = self.definnermodel(pathidxs) # ? scores: float(batsize, vocabsize)
        probs = T.nnet.softmax(scores) # row-wise softmax, ? probs: float(batsize, vocabsize)
        return probs, zidx, [pathidxs, zidx]

    def definnermodel(self, pathidxs):
        raise NotImplementedError("use subclass")

    def getreg(self, regf=lambda x: T.sum(x**2), factor=1./2):
        return factor * reduce(lambda x, y: x + y,
                               map(lambda x: regf(x) * self.twreg,
                                   self.ownparams))

    def geterr(self, probs, gold): # cross-entropy
        return -T.mean(T.log(probs[T.arange(self.batsize), gold]))

    @property
    def ownparams(self):
        return []

    @property
    def depparams(self):
        return []

    def getnormf(self):
        return None

    def getsamplegen(self, trainX, labels):
        batsize = self.batsize

        def samplegen():
            nonzeroidx = sorted(np.random.randint(0, trainX.shape[0], size=(batsize,)).astype("int32"))
            trainXsample = trainX[nonzeroidx, :].astype("int32")
            labelsample = labels[nonzeroidx].astype("int32")
            return [trainXsample, labelsample]     # start + path, target, bad_target
        return samplegen

    def getpredictfunction(self):
        probs, gold, inps = self.defmodel()
        score = probs[T.arange(gold.shape[0]), gold]
        scoref = theano.function(inputs=[inps[0], inps[1]], outputs=score)
        def pref(path, o):
            args = [np.asarray(i).astype("int32") for i in [path, o]]
            return scoref(*args)
        return pref


class SMSM(KMSM):

    def defmodel(self):
        pathidxs = T.imatrix("pathidxs")  # integers of (batsize, seqlen)
        zidxs = T.imatrix("zidxs") # integers of (batsize, seqlen)
        occluder = T.imatrix("occluder")
        scores = self.definnermodel(pathidxs) #predictions, floats of (batsize, seqlen, vocabsize)
        #probs = T.nnet.softmax(scores) # row-wise softmax; probs: (batsize, seqlen, vocabsize) #softmax doesn't work on tensor3D
        probs, _ = theano.scan(fn=T.nnet.softmax,
                            sequences=scores,
                            outputs_info=[None])
        return probs, zidxs, occluder, [pathidxs, zidxs, occluder]

    def geterr(self, *args): # cross-entropy; probs: floats of (batsize, seqlen, vocabsize), gold: indexes of (batsize, seqlen)
        probs = args[0]
        golds = args[1]
        occluder = args[2]
        return -T.sum(
                    occluder *
                    T.log(
                        probs[T.arange(probs.shape[0])[:, None],
                              T.arange(probs.shape[1])[None, :],
                              golds])) / occluder.norm(1) # --> prob: floats of (batsize, seqlen) # is mean of logs of all matrix elements correct?

    def getsamplegen(self, trainX, labels): # trainX and labels must be of same dimensions
        batsize = self.batsize

        def samplegen():
            nonzeroidx = sorted(np.random.randint(0, trainX.shape[0], size=(batsize,)).astype("int32"))
            trainXsample = trainX[nonzeroidx, :].astype("int32")
            labelsample = labels[nonzeroidx, :].astype("int32")
            occluder = (labelsample > 0).astype("int32")
            return [trainXsample, labelsample, occluder]     # input seq, output seq
        return samplegen

    def getprobfunction(self): # occlusion is ignored
        probs, golds, occ, inps = self.defmodel()
        probs = probs[:, -1, :]
        scoref = theano.function(inputs=[inps[0]], outputs=probs)
        def probf(paths, occl=None):
            arg = np.asarray(paths).astype("int32")
            probsvals = scoref(arg)
            return probsvals
        return probf

    def getpredictfunction(self): # TOTEST
        probf = self.getprobfunction()
        def pref(path, o, occl=None):
            probvals = probf(path, occl)
            return probvals[np.arange(probvals.shape[0]), o]
        return pref

    def getsamplefunction(self):
        probf = self.getprobfunction()
        def samplef(path, occl=None):
            arg = np.asarray(path).astype("int32")
            probsvals = probf(arg, occl)
            ret = []
            for i in range(arg.shape[0]): #iterate over examples
                ret.append(np.random.choice(np.arange(probsvals.shape[1]), p=probsvals[i, :]))
            return ret
        return samplef

    def genseq(self, start, endsym):
        samplef = self.getsamplefunction()
        seq = [[]]
        current = start
        seq[0].append(current)
        while current != endsym:
            next = samplef(seq)[0]
            seq[0].append(next)
            current = next
        return seq

class ESMSM(SMSM, Normalizable): # identical to EKMSM since the same prediction part
    def __init__(self, dim=10, **kw):
        super(ESMSM, self).__init__(**kw)
        offset=0.5
        scale=1.
        self.dim = dim
        self.W = theano.shared((np.random.random((self.vocabsize, self.dim)).astype("float32")-offset)*scale, name="W")

    def getnormf(self):
        if self._normalize is True:
            norms = self.W.norm(2, axis=1).reshape((self.W.shape[0], 1))
            upd = (self.W, self.W/norms)
            return theano.function(inputs=[], outputs=[], updates=[upd])
        else:
            return None

    @property
    def printname(self):
        return super(ESMSM, self).printname + "+E" + str(self.dim)+"D"

    @property
    def ownparams(self):
        return [self.W]

    @property
    def depparams(self):
        return []

    def embed(self, *idxs):
        return tuple(map(lambda x: self.W[x, :], idxs))

    def definnermodel(self, pathidxs):
        pathembs, = self.embed(pathidxs) # pathembs: (batsize, seqlen, edim); zemb: (batsize, edim)
        return self.innermodel(pathembs)

    def innermodel(self, pathembs):
        raise NotImplementedError("use subclass")

class RNNESMSM(ESMSM):

    @classmethod
    def loadfrom(cls, src):
        self = cls()
        self.W = src.W
        self.Wout = src.Wout
        self.rnnu = src.rnnu
        self.vocabsize = src.vocabsize
        self.batsize = src.batsize
        return self

    def innermodel(self, pathembs): #pathemb: (batsize, seqlen, dim)
        oseq = self.rnnu(pathembs) # oseq is (batsize, seqlen, innerdims)  ---> last output
        scores = T.dot(oseq, self.Wout) # --> (batsize, seqlen, vocabsize)
        return scores

    @property
    def printname(self):
        return super(RNNESMSM, self).printname + "+" + self.rnnu.__class__.__name__+ ":" + str(self.rnnu.innerdim) + "D"

    @property
    def depparams(self):
        return self.rnnu.parameters

    def __add__(self, other):
        if isinstance(other, RNUBase):
            self.rnnu = other
            self.onrnnudefined()
            return self
        else:
            return super(RNNESMSM, self).__add__(other)


    @property
    def ownparams(self):
        return super(RNNESMSM, self).ownparams + [self.Wout]

    def onrnnudefined(self):
        self.initwout()

    def initwout(self):
        offset = 0.5
        scale = 0.1
        self.Wout = theano.shared((np.random.random((self.rnnu.innerdim, self.vocabsize)).astype("float32")-offset)*scale, name="Wout")


class RNNESMSMShort(RNNESMSM):
    def __init__(self, occlusion=0.1, **kw): # randomly occlude portion of sequence elements
        self.occlusion = occlusion
        super(RNNESMSMShort, self).__init__(**kw)

    def defmodel(self):
        pathidxs = T.imatrix("pathidxs")  # integers of (batsize, seqlen)
        zidxs = T.imatrix("zidxs") # integers of (batsize, seqlen)
        occlusion = T.fmatrix("occlusion") # (batsize, seqlen)

        numstates = len(inspect.getargspec(self.rnnu.rec).args) - 2
        initstate = T.zeros((pathidxs.shape[0], self.rnnu.innerdim))
        initstate2 = T.zeros((pathidxs.shape[0], self.vocabsize))
        outputs, _ = theano.scan(fn=self.step, # --> iterate over seqlen
                                 sequences=[pathidxs.T, occlusion[:, :-1].T],
                                 outputs_info=[None]+[initstate2]+[initstate]*numstates)
        probs = outputs[0].dimshuffle(1, 0, 2)
        return probs, zidxs, occlusion, [pathidxs, zidxs, occlusion]

    def step(self, pathidx, occlusion, prevout, *rnustates): # pathidx, occlusion: (batsize,),   each state: (batsize, innerdim) #TODO: check dims
        pathemb, = self.embed(pathidx) # (batsize, dim)
        inp = (occlusion*pathemb.T).T + ((T.ones_like(occlusion)-occlusion)*T.dot(prevout, self.W).T).T
        rnnuouts = self.rnnu.rec(inp, *rnustates)
        rnuout = rnnuouts[0]
        rnnstates = rnnuouts[1:]
        newprevout = T.nnet.softmax(T.dot(rnuout, self.Wout)) # (batsize, vocabsize)
        probs = newprevout
        return [probs, newprevout] + rnnstates

    def geterr(self, probs, golds, occlusion): # cross-entropy; probs: floats of (batsize, seqlen, vocabsize), gold: indexes of (batsize, seqlen)
        r = occlusion[:, 1:] * T.log(probs[T.arange(probs.shape[0])[:, None],
                                           T.arange(probs.shape[1])[None, :],
                                           golds]) # --> result: floats of (batsize, seqlen)
        return -T.sum(r)/occlusion[:, 1:].norm(1)


    def getsamplegen(self, trainX, labels): # trainX and labels must be of same dimensions
        batsize = self.batsize
        occlprob = self.occlusion

        def samplegen():
            nonzeroidx = sorted(np.random.randint(0, trainX.shape[0], size=(batsize,)).astype("int32"))
            trainXsample = trainX[nonzeroidx, :].astype("int32")
            labelsample = labels[nonzeroidx, :].astype("int32")
            occluder = (trainXsample > 0).astype("int32")
            occlusion = np.random.choice([0, 1], size=trainXsample.shape, p=[occlprob, 1-occlprob]).astype("float32")
            occlusion = occluder * occlusion
            lastoccluder = np.expand_dims((labelsample[:, -1] > 0).astype("float32"), 1)
            occlusion = np.append(occlusion, lastoccluder, axis=1).astype("float32")
            return [trainXsample, labelsample, occlusion]     # input seq, output seq
        return samplegen

    def getprobfunction(self):
        probs, golds, occ, inps = self.defmodel()
        probs = probs[:, -1, :] # last output --> (numsam, vocabsize)
        scoref = theano.function(inputs=[inps[0], inps[2]], outputs=probs)
        def probf(paths, occl=None):
            if occl is None:
                occl = np.ones_like(paths)
            paths = np.asarray(paths).astype("int32")
            occl = np.asarray(occl).astype("float32")
            assert paths.shape == occl.shape
            occl = np.append(occl, np.expand_dims(np.ones_like(occl[:, 0]), axis=1), axis=1).astype("float32")
            probsvals = scoref(paths, occl)
            return probsvals
        return probf




class AutoRNNESMSMShort(RNNESMSMShort): # also inherit from AutoRNNESMSM
    def step(self, pathidx, occlusion, prevout, *rnustates): # pathidx, occlusion: (batsize,),   each state: (batsize, innerdim) #TODO: check dims
        pathemb, = self.embed(pathidx) # (batsize, dim)
        inp = (occlusion*pathemb.T).T + ((1-occlusion)*T.dot(prevout, self.W).T).T
        rnnuouts = self.rnnu.rec(inp, *rnustates)
        rnuout = rnnuouts[0]
        rnnstates = rnnuouts[1:]
        newprevout = T.dot(rnuout, self.Wout) # (batsize, edim)
        probs = T.nnet.softmax(T.dot(newprevout, self.W.T)) # (batsize, vocabsize)
        return [probs, newprevout] + rnnstates


class EKMSM(KMSM, Normalizable):
    def __init__(self, dim=10, **kw):
        super(EKMSM, self).__init__(**kw)
        offset=0.5
        scale=1.
        self.dim = dim
        self.W = theano.shared((np.random.random((self.vocabsize, self.dim)).astype("float32")-offset)*scale, name="W")

    def getnormf(self):
        if self._normalize is True:
            norms = self.W.norm(2, axis=1).reshape((self.W.shape[0], 1))
            upd = (self.W, self.W/norms)
            return theano.function(inputs=[], outputs=[], updates=[upd])
        else:
            return None

    @property
    def printname(self):
        return super(EKMSM, self).printname + "+E" + str(self.dim)+"D"

    @property
    def ownparams(self):
        return [self.W]

    @property
    def depparams(self):
        return []

    def embed(self, *idxs):
        return tuple(map(lambda x: self.W[x, :], idxs))

    def definnermodel(self, pathidxs):
        pathembs, = self.embed(pathidxs) # pathembs: (batsize, seqlen, edim); zemb: (batsize, edim)
        return self.innermodel(pathembs)

    def innermodel(self, pathembs):
        raise NotImplementedError("use subclass")

class RNNEKMSM(EKMSM):

    def innermodel(self, pathembs): #pathemb: (batsize, seqlen, dim)
        oseq = self.rnnu(pathembs)
        om = oseq[:, -1, :] # om is (batsize, innerdims)  ---> last output
        scores = T.dot(om, self.Wout) # --> (batsize, vocabsize)
        return scores

    @property
    def printname(self):
        return super(RNNEKMSM, self).printname + "+" + self.rnnu.__class__.__name__+ ":" + str(self.rnnu.innerdim) + "D"

    @property
    def depparams(self):
        return self.rnnu.parameters

    def __add__(self, other):
        if isinstance(other, RNUBase):
            self.rnnu = other
            self.onrnnudefined()
            return self
        else:
            return super(EKMSM, self).__add__(other)


    @property
    def ownparams(self):
        return super(RNNEKMSM, self).ownparams + [self.Wout]

    def onrnnudefined(self):
        self.initwout()

    def initwout(self):
        offset = 0.5
        scale = 0.1
        self.Wout = theano.shared((np.random.random((self.rnnu.innerdim, self.vocabsize)).astype("float32")-offset)*scale, name="Wout")

class AutoRNNEKMSM(RNNEKMSM):
    def innermodel(self, pathembs): #pathemb: (batsize, seqlen, dim)
        oseq = self.rnnu(pathembs)
        om = oseq[:, -1, :] # om is (batsize, innerdims)  ---> last output
        scores = T.dot(om, self.Wout) # --> (batsize, dim)
        scores = T.dot(scores, self.W.T) # --> (batsize, vocabsize)
        return scores

    def initwout(self):
        offset = 0.5
        scale = 0.1
        self.Wout = theano.shared((np.random.random((self.rnnu.innerdim, self.dim)).astype("float32")-offset)*scale, name="Wout")


class KMM(SGDBase, Predictor, Profileable, Saveable):
    def __init__(self, vocabsize=10, negrate=1, margin=1.0, **kw):
        super(KMM, self).__init__(**kw)
        self.vocabsize = vocabsize
        self.negrate = negrate
        self.margin = margin

    @property
    def printname(self):
        return super(KMM, self).printname + "+n"+str(self.negrate)

    def train(self, trainX, labels, evalinter=10): # X: z, x, y, v OR r, s, o, v
        self.batsize = int(ceil(trainX.shape[0]*1./self.numbats))
        self.tbatsize = theano.shared(np.int32(self.batsize))
        pdot, ndot, inps = self.defmodel()
        tErr = self.geterr(pdot, ndot)
        tReg = self.getreg()
        #embed()
        tCost = tErr + tReg
        #showgraph(tCost)
        #embed() # tErr.eval({inps[0]: [0], inps[1]:[10], gold: [1]})

        trainf = self.gettrainf(inps, [tErr, tCost], tCost)
        err = self.trainloop(trainf=self.getbatchloop(trainf, self.getsamplegen(trainX, labels)),
                             evalinter=evalinter,
                             normf=self.getnormf())
        return err

    def defmodel(self):
        pathidxs = T.imatrix("pathidxs")
        zidx, nzidx = T.ivectors("zidx", "nzidx") # rhs corruption only
        dotp, ndotp = self.definnermodel(pathidxs, zidx, nzidx)
        return dotp, ndotp, [pathidxs, zidx, nzidx]

    def definnermodel(self, pathidxs, zidx, nzidx):
        raise NotImplementedError("use subclass")

    def getreg(self, regf=lambda x: T.sum(x**2), factor=1./2):
        return factor * reduce(lambda x, y: x + y,
                               map(lambda x: regf(x) * self.twreg,
                                   self.ownparams))

    def geterr(self, pdot, ndot): # max margin
        comp = T.clip(self.margin - pdot + ndot, 0, np.infty)
        return T.sum(comp)

    @property
    def ownparams(self):
        return []

    @property
    def depparams(self):
        return []

    def getnormf(self):
        return None

    def getsamplegen(self, trainX, labels):
        batsize = self.batsize
        negrate = self.negrate

        def samplegen():
            nonzeroidx = sorted(np.random.randint(0, trainX.shape[0], size=(batsize,)).astype("int32"))
            trainXsample = trainX[nonzeroidx, :].astype("int32")
            trainXsample = np.repeat(trainXsample, negrate, axis=0)
            labelsample = labels[nonzeroidx].astype("int32")
            labelsample = np.repeat(labelsample, negrate, axis=0)
            corruptedlabels = np.random.randint(0, self.vocabsize, size=(batsize,)).astype("int32")
            for i in range(negrate-1):
                corruptedlabels = np.append(corruptedlabels, np.random.randint(0, self.vocabsize, size=(batsize,)).astype("int32"), axis=0)
            return [trainXsample, labelsample, corruptedlabels]     # start + path, target, bad_target
        return samplegen

    def getpredictfunction(self):
        pdot, _, inps = self.defmodel()
        scoref = theano.function(inputs=[inps[0], inps[1]], outputs=pdot)
        def pref(path, o):
            args = [np.asarray(i).astype("int32") for i in [path, o]]
            return scoref(*args)
        return pref


class EKMM(KMM, Normalizable):
    def __init__(self, dim=10, **kw):
        super(EKMM, self).__init__(**kw)
        offset = 0.5
        scale = 1.
        self.dim = dim
        self.W = theano.shared((np.random.random((self.vocabsize, self.dim)).astype("float32")-offset)*scale, name="W")

    def getnormf(self):
        if self._normalize is True:
            norms = self.W.norm(2, axis=1).reshape((self.W.shape[0], 1))
            upd = (self.W, self.W/norms)
            return theano.function(inputs=[], outputs=[], updates=[upd])
        else:
            return None

    @property
    def printname(self):
        return super(EKMM, self).printname + "+E" + str(self.dim)+"D"

    @property
    def ownparams(self):
        return [self.W]

    @property
    def depparams(self):
        return []

    def embed(self, *idxs):
        return tuple(map(lambda x: self.W[x, :], idxs))

    def definnermodel(self, pathidxs, zidx, nzidx):
        pathembs, zemb, nzemb = self.embed(pathidxs, zidx, nzidx)
        return self.innermodel(pathembs, zemb, nzemb)

    def innermodel(self, pathembs, zemb, nzemb):
        raise NotImplementedError("use subclass")


class AddEKMM(EKMM):
    # TransE

    def innermodel(self, pathembs, zemb, nzemb): #pathemb: (batsize, seqlen, dim)
        om, _ = theano.scan(fn=self.traverse,
                         sequences=pathembs.dimshuffle(1, 0, 2), # --> (seqlen, batsize, dim)
                         outputs_info=[None, self.emptystate(pathembs)] # zeroes like (batsize, dim)
                         )
        om = om[0] # --> (seqlen, batsize, dim)
        om = om[-1, :, :] # --> (batsize, dim)
        dotp = self.membership(om, zemb)
        ndotp = self.membership(om, nzemb)
        return dotp, ndotp

    def emptystate(self, pathembs):
        return T.zeros_like(pathembs[:, 0, :])

    def traverse(self, x_t, h_tm1):
        h = h_tm1 + x_t
        return [h, h]

    def membership(self, o, t):
        return -T.sum(T.sqr(o - t), axis=1)


class DiagMulEKMM(AddEKMM):
    # Bilinear Diag
    def traverse(self, x_t, h_tm1):
        h = x_t * h_tm1
        return [h, h]

    def membership(self, o, t):
        return T.batched_dot(o, t)

    def emptystate(self, pathembs):
        return T.ones_like(pathembs[:, 0, :])

class MulEKMM(EKMM):
    pass # TODO: RESCAL
    # __init__: initialize relation embeddings tensor R, require relational offset for index
    # def definnermodel (embed relation using relation tensor R, entities using W)
    # def innermodel: set initial state to subject entity embedding, then theano.scan with traverse over all relation matrices
    # def traverse: apply relation matrix
    # def membership: dot product with object embedding
    # def ownparams: [self.W, self.R]


class RNNEKMM(EKMM):

    def innermodel(self, pathembs, zemb, nzemb): #pathemb: (batsize, seqlen, dim)
        oseq = self.rnnu(pathembs)
        om = oseq[:, -1, :] # om is (batsize, innerdims)  ---> last output
        dotp = self.membership_dot(om, zemb)
        ndotp = self.membership_dot(om, nzemb)
        return dotp, ndotp

    def membership_dot(self, o, t):
        return T.batched_dot(o, t)

    def membership_add(self, o, t):
        return -T.sum(T.sqr(o - t), axis=1)

    @property
    def printname(self):
        return super(RNNEKMM, self).printname + "+" + self.rnnu.__class__.__name__

    @property
    def depparams(self):
        return self.rnnu.parameters

    def __add__(self, other):
        if isinstance(other, RNUBase):
            self.rnnu = other
            self.onrnnudefined()
            return self
        else:
            return super(EKMM, self).__add__(other)

    def onrnnudefined(self):
        pass


class RNNEOKMM(RNNEKMM):
    def onrnnudefined(self):
        self.initwout()

    def initwout(self):
        offset = 0.5
        scale = 0.1
        self.Wout = theano.shared((np.random.random((self.rnnu.innerdim, self.dim)).astype("float32")-offset)*scale, name="Wout")

    def membership_dot(self, o, t):
        om = T.dot(o, self.Wout)
        return T.batched_dot(om, t)

    def membership_add(self, o, t):
        om = T.dot(o, self.Wout)
        return -T.sum(T.sqr(om - t), axis=1)

    @property
    def ownparams(self):
        return super(RNNEOKMM, self).ownparams + [self.Wout]

    @property
    def printname(self):
        return super(RNNEKMM, self).printname + "+" + self.rnnu.__class__.__name__ + ":" + str(self.rnnu.innerdim) + "D"


def showgraph(var):
    pass
    #theano.printing.pydotprint(var, outfile="/home/denis/logreg_pydotprint_prediction.png", var_with_name_simple=True)

if __name__ == "__main__":
    pass