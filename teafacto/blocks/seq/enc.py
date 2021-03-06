from teafacto.blocks.basic import IdxToOneHot, VectorEmbed
from teafacto.blocks.pool import Pool
from teafacto.blocks.seq.rnn import MakeRNU
from teafacto.blocks.seq.oldseqproc import Vec2Idx, SimpleVec2Idx
from teafacto.blocks.seq.rnn import SeqEncoder, MaskMode
from teafacto.core.base import Block, tensorops as T
from teafacto.util import issequence


class SeqUnroll(Block):
    def __init__(self, block, **kw):
        self.inner = block
        super(SeqUnroll, self).__init__(**kw)

    def apply(self, seq):   # (batsize, seqlen, ...)
        x = seq.dimswap(1, 0)
        ret, _ = T.scan(self.rec, sequences=x)
        return ret.dimswap(1, 0)

    def rec(self, *args, **kwargs):
        return self.inner(*args, **kwargs)


class Seq2Idx(Block):
    def __init__(self, seq2vec, vec2idx, **kw):
        self.enc = seq2vec
        self.out = vec2idx
        super(Seq2Idx, self).__init__(**kw)

    def apply(self, x, mask=None):         # x: idx^(batsize, seqlen)
        enco = self.enc(x, mask=mask)      # (batsize, innerdim)
        out = self.out(enco)    # (batsize, probs)
        return out


class LayerSeq2Idx(Seq2Idx):
    def __init__(self, inpemb, enclayers, outlayers, maskid=0, **kw):
        enc = Seq2Vec(inpemb, enclayers, maskid)
        out = Vec2Idx(outlayers)
        super(LayerSeq2Idx, self).__init__(enc, out, **kw)


class SimpleSeq2Idx(Seq2Idx):
    def __init__(self, indim=400, outdim=100, inpembdim=50, innerdim=100, maskid=0, bidir=False, **kw):
        enc = SimpleSeq2Vec(indim=indim, inpembdim=inpembdim, innerdim=innerdim, maskid=0, bidir=bidir)
        out = SimpleVec2Idx(indim=enc.outdim, outdim=outdim)
        super(SimpleSeq2Idx, self).__init__(enc, out, **kw)


class Seq2Vec(Block):
    def __init__(self, inpemb, enclayers, maskid=0, pool=None, **kw):
        super(Seq2Vec, self).__init__(**kw)
        self.maskid = maskid
        self.inpemb = inpemb
        if not issequence(enclayers):
            enclayers = [enclayers]
        self.pool = pool
        self.enc = SeqEncoder(inpemb, *enclayers).maskoptions(maskid, MaskMode.AUTO)
        if self.pool is not None:
            self.enc = self.enc.all_outputs

    def apply(self, x, mask=None):
        ret = self.enc(x, mask=mask)
        if self.pool is not None:
            ret = self.pool(ret)
        return ret


class SimpleSeq2Vec(Seq2Vec):
    def __init__(self, indim=400, inpembdim=50, inpemb=None, innerdim=100, maskid=0, bidir=False, pool=False, **kw):
        if inpemb is None:
            if inpembdim is None:
                inpemb = IdxToOneHot(indim)
                inpembdim = indim
            else:
                inpemb = VectorEmbed(indim=indim, dim=inpembdim)
        rnn, lastdim = self.makernu(inpembdim, innerdim, bidir=bidir)
        self.outdim = lastdim
        poolblock = None if pool is False else Pool((None,), axis=(1,), mode="max")
        super(SimpleSeq2Vec, self).__init__(inpemb, rnn, maskid, pool=poolblock, **kw)

    @staticmethod
    def makernu(inpembdim, innerdim, bidir=False):
        return MakeRNU.make(inpembdim, innerdim, bidir=bidir)


