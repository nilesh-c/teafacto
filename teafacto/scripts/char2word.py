import sys, re, os.path, numpy as np
from IPython import embed
from teafacto.util import argprun, tokenize, ticktock
from teafacto.blocks.match import MatchScore, CosineDistance
from teafacto.blocks.lang.wordvec import Glove
from teafacto.blocks.seqproc import SeqEncoder, SimpleSeq2Vec


def run(
        epochs=10,
        numbats=100,
        negrate=1,
        lr=0.1,
        embdim=50,
        encdim=50,
        wreg=0.00005,
        margin=1.
    ):
    tt = ticktock("script")
    # get glove words
    g = Glove(encdim)
    words = g.D.keys()
    maxwordlen = 0
    for word in words:
        maxwordlen = max(maxwordlen, len(word))
    chars = set("".join(words))
    print "{} words, maxlen {}, {} characters in words".format(len(words), maxwordlen, len(chars))
    # get char word matrix
    chardic = dict(zip(chars, range(len(chars))))
    charwordmat = -np.ones((len(words), maxwordlen), dtype="int32")
    for i in range(len(words)):
        word = words[i]
        charwordmat[i, :len(word)] = [chardic[x] for x in word]
    print charwordmat[0]
    # encode characters
    cwenc = SimpleSeq2Vec(indim=len(chars),
                          inpembdim=embdim,
                          innerdim=encdim/2,
                          maskid=-1,
                          bidir=True)
    scorer = MatchScore(cwenc, g.block, scorer=CosineDistance())

    class NegIdxGen(object):
        def __init__(self, rng):
            self.min = 0
            self.max = rng

        def __call__(self, datas, gold):
            return datas, np.random.randint(self.min, self.max, gold.shape).astype("int32")

    obj = lambda p, n: (n-p+margin).clip(0, np.infty)

    nscorer = scorer.nstrain([charwordmat, np.arange(len(words))])\
        .negsamplegen(NegIdxGen(len(words))).negrate(negrate)\
        .objective(obj).adagrad(lr=lr).l2(wreg).grad_total_norm(1.0)\
        .train(numbats=numbats, epochs=epochs)



if __name__ == "__main__":
    argprun(run)