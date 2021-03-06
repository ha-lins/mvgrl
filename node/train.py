import copy

import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
import torch.nn.functional as F
from dataset import load

from utils import sparse_mx_to_torch_sparse_tensor


# Borrowed from https://github.com/PetarV-/DGI
class GCN(nn.Module):
    def __init__(self, in_ft, out_ft, bias=True):
        super(GCN, self).__init__()
        self.fc = nn.Linear(in_ft, out_ft, bias=False)
        self.act = nn.PReLU()

        if bias:
            self.bias = nn.Parameter(torch.FloatTensor(out_ft))
            self.bias.data.fill_(0.0)
        else:
            self.register_parameter('bias', None)

        for m in self.modules():
            self.weights_init(m)

    def weights_init(self, m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    # Shape of seq: (batch, nodes, features)
    def forward(self, seq, adj, sparse=False):
        seq_fts = self.fc(seq)  # X * theta
        if sparse:
            out = torch.unsqueeze(torch.spmm(adj, torch.squeeze(seq_fts, 0)), 0)
        else:
            out = torch.bmm(adj, seq_fts)  # A * X * theta
        if self.bias is not None:
            out += self.bias
        return self.act(out)


# Borrowed from https://github.com/PetarV-/DGI
class Readout(nn.Module):
    def __init__(self):
        super(Readout, self).__init__()

    def forward(self, seq, msk):
        if msk is None:
            return torch.mean(seq, 1)
        else:
            msk = torch.unsqueeze(msk, -1)
            return torch.mean(seq * msk, 1) / torch.sum(msk)


# Borrowed from https://github.com/PetarV-/DGI
class Discriminator(nn.Module):
    def __init__(self, n_h):
        super(Discriminator, self).__init__()
        self.f_k = nn.Bilinear(n_h, n_h, 1)

        for m in self.modules():
            self.weights_init(m)

    def weights_init(self, m):
        if isinstance(m, nn.Bilinear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    def forward(self, c1, c2, h1, h2, h3, h4, s_bias1=None, s_bias2=None):
        # h1: [bs, sample_size, sample_size]
        c_x1 = torch.unsqueeze(c1, 1)
        c_x1 = c_x1.expand_as(h1).contiguous()
        c_x2 = torch.unsqueeze(c2, 1)
        c_x2 = c_x2.expand_as(h2).contiguous()

        # positive
        sc_1 = torch.squeeze(self.f_k(h2, c_x1), 2)  # 2-layer MLP
        sc_2 = torch.squeeze(self.f_k(h1, c_x2), 2)

        # negetive
        sc_3 = torch.squeeze(self.f_k(h4, c_x1), 2)
        sc_4 = torch.squeeze(self.f_k(h3, c_x2), 2)

        logits = torch.cat((sc_1, sc_2, sc_3, sc_4), 1)
        return logits


class Model(nn.Module):
    def __init__(self, n_in, n_h):
        super(Model, self).__init__()
        self.gcn1 = GCN(n_in, n_h)
        self.gcn2 = GCN(n_in, n_h)
        self.read = Readout()

        self.sigm = nn.Sigmoid()

        self.disc = Discriminator(n_h)

    def forward(self, seq1, seq2, adj, diff, sparse, msk, samp_bias1, samp_bias2):
        h_1 = self.gcn1(seq1, adj, sparse)
        c_1 = self.read(h_1, msk)  # graph pooling (readout) function
        c_1 = self.sigm(c_1)

        h_2 = self.gcn2(seq1, diff, sparse)
        c_2 = self.read(h_2, msk)
        c_2 = self.sigm(c_2)

        h_3 = self.gcn1(seq2, adj, sparse)
        h_4 = self.gcn2(seq2, diff, sparse)

        ret = self.disc(c_1, c_2, h_1, h_2, h_3, h_4, samp_bias1, samp_bias2)

        return ret, h_1, h_2

    def embed(self, seq, adj, diff, sparse, msk):
        h_1 = self.gcn1(seq, adj, sparse)
        c = self.read(h_1, msk)

        h_2 = self.gcn2(seq, diff, sparse)
        return (h_1 + h_2).detach(), c.detach()


class LogReg(nn.Module):
    def __init__(self, ft_in, nb_classes):
        super(LogReg, self).__init__()
        self.fc = nn.Linear(ft_in, nb_classes)
        self.sigm = nn.Sigmoid()

        for m in self.modules():
            self.weights_init(m)

    def weights_init(self, m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight.data)
            if m.bias is not None:
                m.bias.data.fill_(0.0)

    def forward(self, seq):
        ret = torch.log_softmax(self.fc(seq), dim=-1)
        return ret


class GraphAttentionLayer(nn.Module):
    def __init__(self, in_features, out_features, dropout, alpha, concat=True):
        super(GraphAttentionLayer, self).__init__()
        self.dropout = dropout
        self.in_features = in_features
        self.out_features = out_features
        self.alpha = alpha
        self.concat = concat

        self.W = nn.Parameter(torch.zeros(size=(in_features, out_features)))
        nn.init.xavier_uniform_(self.W.data, gain=1.414)
        self.a = nn.Parameter(torch.zeros(size=(2 * out_features, 1)))
        nn.init.xavier_uniform_(self.a.data, gain=1.414)

        self.leakyrelu = nn.LeakyReLU(self.alpha)

    def forward(self, input, adj, sig_sample_size):
        h = torch.mm(input, self.W)
        N = h.size()[0]

        a_input = torch.cat([h.repeat(1, N).view(N * N, -1), h.repeat(N, 1)], dim=1).view(N, -1, 2 * self.out_features)
        e = self.leakyrelu(torch.matmul(a_input, self.a).squeeze(2))

        zero_vec = -9e15 * torch.ones_like(e)
        attention = torch.where(adj > 0, e, zero_vec)
        attention = F.softmax(attention, dim=1)
        # attention = F.dropout(attention, self.dropout, training=self.training)
        sig_scores = torch.sum(attention, dim=0)
        # print('[info]sig_scores are:{}'.format(sig_scores))
        sorted, indices = torch.topk(sig_scores, sig_sample_size)

        return indices


def train(dataset, verbose=False):
    nb_epochs = 3000
    patience = 20
    lr = 0.001
    l2_coef = 0.0
    hid_units = 512
    sparse = False

    ori_adj, adj, diff, features, labels, idx_train, idx_val, idx_test = load(dataset)
    adj = copy.deepcopy(np.array(adj))

    ft_size = features.shape[1]
    nb_classes = np.unique(labels).shape[0]

    sample_size = 2500  # number of sampled nodes
    batch_size = 4
    sig_sample_size = 2000

    labels = torch.LongTensor(labels)
    idx_train = torch.LongTensor(idx_train)
    idx_test = torch.LongTensor(idx_test)

    lbl_1 = torch.ones(batch_size, sig_sample_size * 2)
    lbl_2 = torch.zeros(batch_size, sig_sample_size * 2)
    lbl = torch.cat((lbl_1, lbl_2), 1)

    attn_layer = GraphAttentionLayer(ft_size, hid_units, dropout=0.6, alpha=0.2)
    model = Model(ft_size, hid_units)
    optimiser = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=l2_coef)

    if torch.cuda.is_available():
        model.cuda()
        labels = labels.cuda()
        lbl = lbl.cuda()
        idx_train = idx_train.cuda()
        idx_test = idx_test.cuda()
        attn_layer = attn_layer.cuda()

    b_xent = nn.BCEWithLogitsLoss()
    xent = nn.CrossEntropyLoss()
    cnt_wait = 0
    best = 1e9
    best_t = 0

    for epoch in range(nb_epochs):

        idx = np.random.randint(0, adj.shape[-1] - sample_size + 1, batch_size)  # [4] (0, N, size())
        bo, ba, bd, bf = [], [], [], []
        for i in idx:
            # rand_ori_adj = copy.deepcopy(ori_adj[i: i + sample_size, i: i + sample_size])
            # rand_adj = copy.deepcopy(adj[i: i + sample_size, i: i + sample_size])
            # rand_diff = copy.deepcopy(diff[i: i + sample_size, i: i + sample_size])
            # rand_ft = copy.deepcopy(features[i: i + sample_size])
            #
            # # scores = rand_ft.dot(np.transpose(rand_ft)) * rand_ori_adj
            # scores = self_attn(rand_ft, rand_ori_adj)
            # for query, attn in enumerate(scores):
            #     non_ids = np.nonzero(attn)
            #     attn_scores = []
            #     if not len(non_ids[0]):
            #         continue
            #     for key in non_ids:
            #         attn_scores.append(scores[query][key])
            #
            #     norm_scores = softmax(attn_scores)
            #     for i, key in enumerate(non_ids):
            #         scores[query][key] = norm_scores[i]
            # sig_scores = np.sum(scores, axis=0)
            # norm_sig_scores = softmax(sig_scores)
            #
            # idx = np.argpartition(norm_sig_scores, -sig_sample_size)[-sig_sample_size:]
            # sorted_idx = np.sort(idx)
            # sampled_adj = np.zeros((sig_sample_size, sig_sample_size), dtype=np.float64)
            # sampled_diff = np.zeros((sig_sample_size, sig_sample_size), dtype=np.float64)
            # sampled_ft = np.zeros((sig_sample_size, ft_size), dtype=np.float64)
            #
            # for si, i in enumerate(sorted_idx):
            #     for sj, j in enumerate(sorted_idx):
            #         sampled_adj[si][sj] = rand_adj[i][j] if si == sj else 0.0
            #         sampled_diff[si][sj] = rand_diff[i][j]
            #     sampled_ft[si, :] = rand_ft[i, :]

            # ba.append(sampled_adj)
            # bd.append(sampled_diff)
            # bf.append(sampled_ft)

            bo.append(ori_adj[i: i + sample_size, i: i + sample_size])
            ba.append(adj[i: i + sample_size, i: i + sample_size])
            bd.append(diff[i: i + sample_size, i: i + sample_size])
            bf.append(features[i: i + sample_size])

        bo = np.array(bo).reshape(batch_size, sample_size, sample_size)
        ba = np.array(ba).reshape(batch_size, sample_size, sample_size)
        bd = np.array(bd).reshape(batch_size, sample_size, sample_size)
        bf = np.array(bf).reshape(batch_size, sample_size, ft_size)

        # if not epoch:
        if sparse:
            bo = sparse_mx_to_torch_sparse_tensor(sp.coo_matrix(bo))
            ba = sparse_mx_to_torch_sparse_tensor(sp.coo_matrix(ba))
            bd = sparse_mx_to_torch_sparse_tensor(sp.coo_matrix(bd))
        else:
            bo = torch.FloatTensor(bo)
            ba = torch.FloatTensor(ba)
            bd = torch.FloatTensor(bd)

        bf = torch.FloatTensor(bf)
        idx = np.random.permutation(sig_sample_size)  # a permutated sequence for negative samples
        shuf_fts = bf[:, idx, :]

        if torch.cuda.is_available():
            bo = bo.cuda()
            bf = bf.cuda()
            ba = ba.cuda()
            bd = bd.cuda()
            shuf_fts = shuf_fts.cuda()

        sampled_ba = torch.zeros(batch_size, sig_sample_size, sig_sample_size)
        sampled_bd = torch.zeros(batch_size, sig_sample_size, sig_sample_size)
        sampled_bf = torch.zeros(batch_size, sig_sample_size, ft_size)

        for bi in range(batch_size):
            # scores = self_attn(bf[b_i].cpu(), bo[b_i].cpu())
            #
            # for q_i, attn in enumerate(scores):
            #     print('[info] attn:{}'.format(attn))
            #     # print('[info] ori:{}'.format(bo[b_i]))
            #
            #     non_ids = torch.nonzero(attn, as_tuple=True)
            #     attn_scores = []
            #     if not len(non_ids[0]):
            #         continue
            #     for k_j in non_ids[0]:
            #         attn_scores.append(scores[q_i][k_j])
            #
            #     print('[info] former attn_scores:{}'.format(attn_scores))
            #     attn_scores = torch.FloatTensor(attn_scores)
            #     print('[info] latter attn_scores:{}'.format(attn_scores))
            #     norm_scores = F.softmax(attn_scores, dim=1)
            #     print('[info] norm_scores:{}'.format(norm_scores))
            #     for i, k_j in enumerate(non_ids):
            #         scores[q_i][k_j] = norm_scores[i]
            #
            # print('[info] scores:{}'.format(scores))
            # sig_scores = torch.sum(scores)
            # print('[info] sig_scores:'.format(sig_scores))
            # norm_sig_scores = F.softmax(sig_scores)
            #
            # sorted, indices = torch.topk(norm_sig_scores, sig_sample_size)
            # print('[info] sorted:{} and indices:{}'.format(sorted, indices))
            ids = attn_layer(bf[bi], bo[bi], sig_sample_size)

            sampled_ba[bi, :, :] = ba[bi, ids, ids]
            sampled_bd[bi, :, :] = bd[bi, ids, ids]
            sampled_bf[bi, :, :] = bf[bi, ids, :]

        if torch.cuda.is_available():
            sampled_ba = sampled_ba.cuda()
            sampled_bd = sampled_bd.cuda()
            sampled_bf = sampled_bf.cuda()


        model.train()
        optimiser.zero_grad()

        logits, __, __ = model(sampled_bf, shuf_fts, sampled_ba, sampled_bd, sparse, None, None, None)
        # logits, __, __ = model(bf, shuf_fts, ba, bd, sparse, None, None, None)

        loss = b_xent(logits, lbl)

        loss.backward()
        optimiser.step()

        if verbose:
            print('Epoch: {0}, Loss: {1:0.4f}'.format(epoch, loss.item()))

        if loss < best:
            best = loss
            best_t = epoch
            cnt_wait = 0
            torch.save(model.state_dict(), 'model.pkl')
        else:
            cnt_wait += 1

        if cnt_wait == patience:
            if verbose:
                print('Early stopping!')
            break
        # print(epoch, loss)

    if verbose:
        print('Loading {}th epoch'.format(best_t))
    model.load_state_dict(torch.load('model.pkl'))

    if sparse:
        adj = sparse_mx_to_torch_sparse_tensor(sp.coo_matrix(adj))
        diff = sparse_mx_to_torch_sparse_tensor(sp.coo_matrix(diff))

    features = torch.FloatTensor(features[np.newaxis])
    adj = torch.FloatTensor(adj[np.newaxis])
    diff = torch.FloatTensor(diff[np.newaxis])
    features = features.cuda()
    adj = adj.cuda()
    diff = diff.cuda()

    embeds, _ = model.embed(features, adj, diff, sparse, None)
    train_embs = embeds[0, idx_train]
    test_embs = embeds[0, idx_test]

    train_lbls = labels[idx_train]
    test_lbls = labels[idx_test]

    accs = []
    wd = 0.01 if dataset == 'citeseer' else 0.0

    for _ in range(50):
        log = LogReg(hid_units, nb_classes)
        opt = torch.optim.Adam(log.parameters(), lr=1e-2, weight_decay=wd)
        log.cuda()
        for _ in range(300):
            log.train()
            opt.zero_grad()

            logits = log(train_embs)
            loss = xent(logits, train_lbls)

            loss.backward()
            opt.step()

        logits = log(test_embs)
        preds = torch.argmax(logits, dim=1)
        acc = torch.sum(preds == test_lbls).float() / test_lbls.shape[0]
        accs.append(acc * 100)

    accs = torch.stack(accs)
    print(accs.mean().item(), accs.std().item())
    # return accs.mean().item(), accs.std().item()


if __name__ == '__main__':
    import warnings

    warnings.filterwarnings("ignore")
    # torch.cuda.set_device(7)

    # 'cora', 'citeseer', 'pubmed'
    dataset = 'cora'
    # final_acc = []
    for __ in range(50):
        # accs, _ = \
        train(dataset)
        # final_acc.append(accs)
    # print(np.mean(final_acc), np.var(final_acc))
