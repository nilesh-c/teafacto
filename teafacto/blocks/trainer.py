from datetime import datetime as dt

import numpy as np
import sys
import theano
from lasagne.objectives import *
from lasagne.regularization import l1, l2
from lasagne.updates import *
from theano import tensor as T

from teafacto.blocks.core import *
from teafacto.blocks.datafeed import DataFeeder, SplitIdxIterator
from teafacto.core.utils import ticktock as TT

class ModelTrainer(object):
    def __init__(self, model, gold):
        self.model = model
        self.goldvar = gold
        self.validsetmode= False
        self.average_err = True # TODO: do we still need this?
        # training settings
        self.objective = None
        self.regularizer = None
        self.optimizer = None
        self.traindata = None
        self.traingold = None
        self.gradconstraints = []
        # validation settings
        self.trainstrategy = self._train_full
        self.validsplits = 0
        self.validrandom = False
        self.validata = None
        self.validgold = None
        self.validation = None
        self.validators = []
        self.tt = TT("FluentTrainer")


    ############################################################################## settings ############################

    ################### LOSSES ##########################

    def _set_objective(self, obj):
        if self.validsetmode is False:
            self.objective = obj
        else:
            self.validators.append(obj)

    def linear_objective(self): # multiplies prediction with gold, assumes prediction is already the loss
                                # (this is for negative sampling models where the training model already computes the loss)
        self._set_objective(lambda x, y: x * y)
        return self

    def cross_entropy(self):
        self._set_objective(lambda probs, gold: -T.log(probs[T.arange(gold.shape[0]), gold]))
        return self

    def squared_error(self):
        self._set_objective(lambda x, y: squared_error(x, y))
        return self

    def hinge_loss(self):
        # TODO
        return self

    # TODO more objectives

    #################### GRADIENT CONSTRAINTS ############ --> applied in the order that they were added
    def grad_total_norm(self, max_norm, epsilon=1e-7):
        self.gradconstraints.append(lambda allgrads: total_norm_constraint(allgrads, max_norm, epsilon=epsilon))
        return self

    def grad_add_constraintf(self, f):
        self.gradconstraints.append(f)
        return self

    def _gradconstrain(self, allgrads):
        ret = allgrads
        for gcf in self.gradconstraints:
            ret = gcf(ret)
        return ret

    # !!! can add more

    #################### REGULARIZERS ####################
    def _regul(self, regf, amount, params):
        return amount * [regf(x.d)*x.regmul for x in params]

    def l2(self, amount):
        self.regularizer = lambda x: self._regul(l2, amount, x)
        return self

    def l1(self, amount):
        self.regularizer = lambda x: self._regul(l1, amount, x)
        return self

    ##################### OPTIMIZERS ######################
    def sgd(self, lr):
        self.optimizer = lambda x, y: sgd(x, y, learning_rate=lr)
        return self

    def momentum(self, lr, mome=0.9):
        self.optimizer = lambda x, y: momentum(x, y, learning_rate=lr, momentum=mome)
        return self

    def nesterov_momentum(self, lr, momentum=0.9):
        self.optimizer = lambda x, y: nesterov_momentum(x, y, learning_rate=lr, momentum=momentum)
        return self

    def adagrad(self, lr=1.0, epsilon=1e-6):
        self.optimizer = lambda x, y: adagrad(x, y, learning_rate=lr, epsilon=epsilon)
        return self

    def rmsprop(self, lr=1., rho=0.9, epsilon=1e-6):
        self.optimizer = lambda x, y: rmsprop(x, y, learning_rate=lr, rho=rho, epsilon=epsilon)
        return self

    def adadelta(self, lr=1., rho=0.95, epsilon=1e-6):
        self.optimizer = lambda x, y: adadelta(x, y, learning_rate=lr, rho=rho, epsilon=epsilon)
        return self

    def adam(self, lr=0.001, b1=0.9, b2=0.999, epsilon=1e-8):
        self.optimizer = lambda x, y: adam(x, y, learning_rate=lr, beta1=b1, beta2=b2, epsilon=epsilon)
        return self

    ################### VALIDATION ####################### --> use one of following

    def validate(self, splits=5, random=False):
        self.trainstrategy = self._train_split
        self.validsplits = splits
        self.validrandom = random
        self.validsetmode = True
        return self

    def validate_on(self, data, gold):
        self.trainstrategy = self._train_validdata
        self.validdata = data
        self.validgold = gold
        self.validsetmode = True
        return self

    def cross_validate(self, splits=5, random=False):
        self.trainstrategy = self._train_cross_valid
        self.validsplits = splits
        self.validrandom = random
        self.validsetmode = True
        return self

    ############################################################# execution ############################################

    ########################## ACTUAL TRAINING #########################
    def traincheck(self):
        assert(self.optimizer is not None)
        assert(self.objective is not None)
        assert(self.traindata is not None)
        assert(self.traingold is not None)

    def train(self, numbats, epochs):
        self.traincheck()
        self.numbats = numbats
        self.maxiter = epochs
        self._train()

    def _train(self):
        model = self.buildmodel()
        trainfun = self.buildtrainfun(model) # to be applied for one batch
        validfun = self.buildvalidfun(model) # to be applied for one batch
        self.tt.tock("compiled training function")
        # train mode: full -> train on all data; split -> split, then train, then valid; data -> valid on validdata; cross -> cross validation
        return self.trainstrategy(model)

    def buildmodel(self):
        self.model.build()
        return self.model

    def buildtrainfun(self, model):
        params = model.output.allparams
        inputs = model.inputs
        loss, newinp = self.buildlosses(model, [self.objective])
        loss = loss[0]
        if newinp is not None:
            inputs = newinp
        if self.regularizer is not None:
            reg = self.regularizer(params)
            cost = loss+reg
        else:
            cost = loss
        grads = T.grad(cost, [x.d for x in params])  # compute gradient
        grads = self._gradconstrain(grads)
        rawupdates = self.optimizer(grads, [x.d for x in params])       # raw updates from optimizer
        updates = map(lambda x: (x[0][0], x[1].constraintf()(x[0][1]*x[1].lrmul)),    # updates penalized by param lrmul
                      zip(rawupdates.items(), [x for x in params]))                         # and constrained by param's info
        trainf = theano.function(inputs=[x.d for x in inputs]+[self.goldvar], outputs=[cost], updates=updates)
        return trainf

    def buildlosses(self, model, objs):
        return [aggregate(obj(model.output.d, self.goldvar), mode='mean' if self.average_err is True else 'sum') for obj in objs], None

    def buildvalidfun(self, model):
        metrics, newinp = self.buildlosses(model, self.validators)
        inputs = newinp if newinp is not None else model.inputs
        if len(metrics) > 0:
            return theano.function(inputs=[x.d for x in inputs] + [self.goldvar], outputs=metrics)
        else:
            return None

    ################### TRAINING STRATEGIES ############
    def _train_full(self, model): # train on all data, no validation
        trainf = self.buildtrainfun(model)
        err, _ = self.trainloop(
                trainf=self.getbatchloop(trainf, DataFeeder(self.traindata, self.traingold).numbats(self.numbats)),
                average_err=self.average_err)
        return err

    def _train_validdata(self, model):
        trainf = self.buildtrainfun(model)
        validf = self.buildvalidfun(model)
        err, verr = self.trainloop(
                trainf=self.getbatchloop(trainf, DataFeeder(self.traindata, self.traingold).numbats(self.numbats)),
                validf=self.getbatchloop(validf, DataFeeder(self.validdata, self.validgold)),
                average_err=self.average_err)
        return err, verr

    def _train_split(self, model):
        trainf = self.buildtrainfun(model)
        validf = self.buildvalidfun(model)
        df = DataFeeder(self.traindata, self.traingold)
        dftrain, dfvalid = df.split(self.validsplits, self.validrandom)
        err, verr = self.trainloop(
                trainf=self.getbatchloop(trainf, dftrain.numbats(self.numbats)),
                validf=self.getbatchloop(validf, dfvalid),
                average_err=self.average_err)
        return err, verr

    def _train_cross_valid(self, model):
        df = DataFeeder(self.traindata, self.traingold)
        splitter = SplitIdxIterator(df.size, split=self.validsplits, random=self.validrandom, folds=self.validsplits)
        err = []
        verr = []
        c = 0
        for splitidxs in splitter:
            trainf = self.buildtrainfun(model)
            validf = self.buildvalidfun(model)
            tf, vf = df.isplit(splitidxs)
            serr, sverr = self.trainloop(
                trainf=self.getbatchloop(trainf, tf.numbats(self.numbats)),
                validf=self.getbatchloop(validf, vf),
                average_err=self.average_err)
            err.append(serr)
            verr.append(sverr)
            self.resetmodel(self.model)
        err = np.asarray(err)
        avgerr = np.mean(err, axis=0)
        verr = np.asarray(verr)
        avgverr = np.mean(verr, axis=0)
        self.tt.tock("done")
        return avgerr, avgverr, err, verr

    def resetmodel(self, model):
        params = model.allparams
        for param in params:
            param.reset()

    ############## TRAINING LOOPS ##################
    def trainloop(self, trainf, validf=None, evalinter=1, average_err=True):
        self.tt.tick("training")
        err = []
        verr = []
        stop = self.maxiter == 0
        self.currentiter = 1
        evalcount = evalinter
        while not stop:
            print("iter %d/%.0f" % (self.currentiter, float(self.maxiter)))
            start = dt.now()
            erre = trainf()
            if self.currentiter == self.maxiter:
                stop = True
            self.currentiter += 1
            err.append(erre)
            if validf is not None and self.currentiter % evalinter == 0: # validate and print
                verre = validf()
                verr.append(verre)
                print "training error: %s \t validation error: %s" % (" - ".join(map(lambda x: "%.3f" % x, erre)), " - ".join(map(lambda x: "%.3f" % x, verre)))
            else:
                print "training error: %s" % " - ".join(map(lambda x: "%.3f" % x, erre))
            print("iter done in %f seconds" % (dt.now() - start).total_seconds())
            evalcount += 1
            if self.autosave:
                self.save(self.model)
        self.tt.tock("trained").tick()
        return err, verr

    def getbatchloop(self, trainf, datafeeder):
        '''
        returns the batch loop, loaded with the provided trainf training function and samplegen sample generator
        '''

        def batchloop():
            c = 0
            prevperc = -1.
            terr = []
            while datafeeder.hasnextbatch():
                #region Percentage counting
                perc = round(c*100./datafeeder.size)
                if perc > prevperc: sys.stdout.write("iter progress %.0f" % perc + "% \r"); sys.stdout.flush(); prevperc = perc
                #endregion
                sampleinps = datafeeder.nextbatch()
                eterr = trainf(*sampleinps)
                if len(terr) == 0: # new
                    terr = eterr
                else:
                    terr = map(lambda x: x[0]+x[1], zip(terr, eterr))
                c += 1
            if self.average_err is True:
                terr = map(lambda x: x*1./c, terr)
            return terr
        return batchloop

    @property
    def autosave(self):
        return False # TODO

    def save(self, model):
        pass # TODO


class ContrastModelTrainer(ModelTrainer):

    def buildlosses(self, model, obj):
        inpblocks = model.inputs # e.g. indexes of s, p, o: 1st dim: examples, 2nd dim: feature values
        # data structure: 1st dim: examples, 2nd dim: pos, neg, neg, neg, 3rd dim: feature values
        # model predicts a score, the loss in trainer operates between the pos and all neg examples
        # TODO: what role does the goldvar play?
        # make new inputs based on model inputs
        newinpblocks = [Input(x.ndim + 1, x.dtype) for x in inpblocks]
        si = [x.dimswap(1, 0).d for x in newinpblocks] # put pos/neg dim as first

        def pair(*args):
            pargs = args[:len(args)/2]
            nargs = args[len(args)/2:]
            pos = model.wrapply(pargs) # --> (batsize,)
            neg = model.wrapply(nargs) # --> (batsize,)
            closses = obj(pos, neg)  # --> (batsize,)
            return closses

        o, _ = theano.scan(fn=pair, sequences=si[1:], non_sequences=si[0]) # iterate over neg examples --> (negrate, batsize)
        aggf = T.mean if self.average_err is True else T.sum
        oa = aggf(o, axis=0)
        oaa = aggf(oa, axis=1)
        return oaa, newinpblocks

