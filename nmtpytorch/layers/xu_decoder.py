# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.nn.functional as F

from ..utils.data import to_var
from ..utils.nn import get_rnn_hidden_state
from . import FF, Attention


class XuDecoder(nn.Module):
    """A decoder which implements Show-attend-and-tell decoder."""
    def __init__(self, input_size, hidden_size, ctx_size_dict, ctx_name, n_vocab,
                 rnn_type, tied_emb=False, dec_init='zero', att_type='mlp',
                 att_activ='tanh', att_bottleneck='ctx',
                 transform_ctx=True, mlp_bias=False, dropout=0,
                 emb_maxnorm=None, emb_gradscale=False, att_temp=1.0,
                 selector=False, prev2out=True, ctx2out=True):
        super().__init__()

        # Normalize case
        self.rnn_type = rnn_type.upper()

        # Safety checks
        assert self.rnn_type in ('GRU', 'LSTM'), \
            "rnn_type '{}' not known".format(rnn_type)
        assert dec_init in ('zero', 'mean_ctx'), \
            "dec_init '{}' not known".format(dec_init)

        RNN = getattr(nn, '{}Cell'.format(self.rnn_type))
        # LSTMs have also the cell state
        self.n_states = 1 if self.rnn_type == 'GRU' else 2

        # Set custom handlers for GRU/LSTM
        if self.rnn_type == 'GRU':
            self._rnn_unpack_states = lambda x: x
            self._rnn_pack_states = lambda x: x
        elif self.rnn_type == 'LSTM':
            self._rnn_unpack_states = self._lstm_unpack_states
            self._rnn_pack_states = self._lstm_pack_states

        # Set decoder initializer
        self._init_func = getattr(self, '_rnn_init_{}'.format(dec_init))

        # Other arguments
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.ctx_size_dict = ctx_size_dict
        self.ctx_name = ctx_name
        self.n_vocab = n_vocab
        self.tied_emb = tied_emb
        self.dec_init = dec_init
        self.att_type = att_type
        self.att_bottleneck = att_bottleneck
        self.att_activ = att_activ
        self.att_temp = att_temp
        self.transform_ctx = transform_ctx
        self.mlp_bias = mlp_bias
        self.dropout = dropout
        self.emb_maxnorm = emb_maxnorm
        self.emb_gradscale = emb_gradscale
        self.selector = selector
        self.prev2out = prev2out
        self.ctx2out = ctx2out

        # Create target embeddings
        self.emb = nn.Embedding(self.n_vocab, self.input_size,
                                padding_idx=0, max_norm=self.emb_maxnorm,
                                scale_grad_by_freq=self.emb_gradscale)

        # Create attention layer
        self.att = Attention(self.ctx_size_dict[self.ctx_name], self.hidden_size,
                             transform_ctx=self.transform_ctx,
                             mlp_bias=self.mlp_bias,
                             att_type=self.att_type,
                             att_activ=self.att_activ,
                             att_bottleneck=self.att_bottleneck,
                             temp=self.att_temp, ctx2hid=False)

        # Decoder initializer FF (for mean_ctx)
        if self.dec_init == 'mean_ctx':
            self.ff_dec_init = FF(
                self.ctx_size_dict[self.ctx_name],
                self.hidden_size * self.n_states, activ='tanh')

        # Create decoder from [y_t, z_t] to dec_dim
        self.dec0 = RNN(self.input_size + self.ctx_size_dict[self.ctx_name],
                        self.hidden_size)

        # Dropout
        if self.dropout > 0:
            self.do = nn.Dropout(p=self.dropout)

        # Output bottleneck: maps hidden states to target emb dim
        self.hid2out = FF(self.hidden_size, self.input_size, activ='tanh')

        # Final softmax
        self.out2prob = FF(self.input_size, self.n_vocab)

        # Gating Scalar, i.e. selector
        if self.selector:
            self.ff_selector = FF(self.hidden_size, 1, activ='sigmoid')

        if self.ctx2out:
            self.ff_out_ctx = FF(
                self.ctx_size_dict[self.ctx_name], self.input_size)

        # Tie input embedding matrix and output embedding matrix
        if self.tied_emb:
            self.out2prob.weight = self.emb.weight

        self.nll_loss = nn.NLLLoss(size_average=False, ignore_index=0)

    def _lstm_pack_states(self, h):
        return torch.cat(h, dim=-1)

    def _lstm_unpack_states(self, h):
        # Split h_t and c_t into two tensors and return a tuple
        return torch.split(h, self.hidden_size, dim=-1)

    def _rnn_init_zero(self, ctx, ctx_mask):
        h_0 = torch.zeros(ctx.shape[1], self.hidden_size * self.n_states)
        return to_var(h_0, requires_grad=False)

    def _rnn_init_mean_ctx(self, ctx, ctx_mask):
        if self.dropout > 0:
            return self.ff_dec_init(self.do(ctx.mean(0)))
        else:
            return self.ff_dec_init(ctx.mean(0))

    def f_init(self, ctx_dict):
        """Returns the initial h_0 for the decoder."""
        return self._init_func(*ctx_dict[self.ctx_name])

    def f_next(self, ctx_dict, y, h):
        # Unpack hidden states
        htm1, *ctm1 = self._rnn_unpack_states(h)

        self.img_alpha_t, z_t = self.att(
            htm1.unsqueeze(0), *ctx_dict[self.ctx_name])

        if self.selector:
            z_t = z_t * self.ff_selector(htm1)

        ht_ct = self.dec0(torch.cat([y, z_t], dim=1), h)
        ht = get_rnn_hidden_state(ht_ct)

        # This is a bottleneck to avoid going from H to V directly
        if self.dropout > 0:
            logit = self.hid2out(self.do(ht))
        else:
            logit = self.hid2out(ht)

        if self.prev2out:
            logit += y

        if self.ctx2out:
            logit += self.ff_out_ctx(z_t)

        # Transform logit to T*B*V (V: vocab_size)
        # Compute log_softmax over token dim
        log_p = F.log_softmax(self.out2prob(F.tanh(logit)), dim=-1)

        # Return log probs and new hidden states
        return log_p, self._rnn_pack_states(ht_ct)

    def forward(self, ctx_dict, y):
        """Computes the softmax outputs given source annotations `ctxs` and
        ground-truth target token indices `y`. Only called during training.

        Arguments:
            ctxs(Variable): A variable of `S*B*ctx_dim` representing the source
                annotations in an order compatible with ground-truth targets.
            y(Variable): A variable of `T*B` containing ground-truth target
                token indices for the given batch.
        """

        loss = 0.0
        logps = None if self.training else torch.zeros(
            y.shape[0] - 1, y.shape[1], self.n_vocab).cuda()

        # Convert token indices to embeddings -> T*B*E
        y_emb = self.emb(y)

        # Get initial hidden state
        h = self.f_init(ctx_dict)

        # -1: So that we skip the timestep where input is <eos>
        for t in range(y_emb.shape[0] - 1):
            log_p, h = self.f_next(ctx_dict, y_emb[t], h)
            if not self.training:
                logps[t] = log_p.data
            loss += self.nll_loss(log_p, y[t + 1])

        return {'loss': loss, 'logps': logps}
