# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from collections import OrderedDict
import os
import copy

from fairseq.data import (
    BacktranslationDataset,
    IndexedCachedDataset,
    IndexedDataset,
    IndexedRawTextDataset,
    LanguagePairDataset,
    NoisingDataset,
    data_utils,
    RoundRobinZipDatasets,
    UnsupervisedMTNoising,
)
from fairseq.models import FairseqMultiModel
from fairseq.sequence_generator import SequenceGenerator
from fairseq.tasks.translation import load_langpair_dataset
from fairseq import utils
from fairseq.optim import lr_scheduler
from fairseq.optim import build_optimizer
import torch

from .multilingual_translation import MultilingualTranslationTask
from . import register_task


def _get_bt_dataset_key(lang_pair):
    return "bt:" + lang_pair


def _get_denoising_dataset_key(lang_pair):
    return "denoising:" + lang_pair

def  _get_dds_bt_key(lang_pair):
    src, tgt = lang_pair.split("-")
    bt_lang_pair = tgt + "-" + src
    return bt_lang_pair

# ported from UnsupervisedMT
def parse_lambda_config(x):
    """
    Parse the configuration of lambda coefficient (for scheduling).
    x = "3"                  # lambda will be a constant equal to x
    x = "0:1,1000:0"         # lambda will start from 1 and linearly decrease
                             # to 0 during the first 1000 iterations
    x = "0:0,1000:0,2000:1"  # lambda will be equal to 0 for the first 1000
                             # iterations, then will linearly increase to 1 until iteration 2000
    """
    split = x.split(',')
    if len(split) == 1:
        return float(x), None
    else:
        split = [s.split(':') for s in split]
        assert all(len(s) == 2 for s in split)
        assert all(k.isdigit() for k, _ in split)
        assert all(int(split[i][0]) < int(split[i + 1][0]) for i in range(len(split) - 1))
        return float(split[0][1]), [(int(k), float(v)) for k, v in split]


@register_task('bt_translation')
class BtTranslationTask(MultilingualTranslationTask):
    """A task for training multiple translation models simultaneously.

    We iterate round-robin over batches from multiple language pairs, ordered
    according to the `--lang-pairs` argument.

    The training loop is roughly:

        for i in range(len(epoch)):
            for lang_pair in args.lang_pairs:
                batch = next_batch_for_lang_pair(lang_pair)
                loss = criterion(model_for_lang_pair(lang_pair), batch)
                loss.backward()
            optimizer.step()

    In practice, `next_batch_for_lang_pair` is abstracted in a FairseqDataset
    (e.g., `RoundRobinZipDatasets`) and `model_for_lang_pair` is a model that
    implements the `FairseqMultiModel` interface.

    During inference it is required to specify a single `--source-lang` and
    `--target-lang`, instead of `--lang-pairs`.
    """

    @staticmethod
    def add_args(parser):
        """Add task-specific arguments to the parser."""
        # fmt: off
        MultilingualTranslationTask.add_args(parser)
        parser.add_argument('--lambda-parallel-config', default="1.0", type=str, metavar='CONFIG',
                            help='cross-entropy reconstruction coefficient (parallel data). '
                                 'use fixed weight during training if set to floating point number. '
                                 'use piecewise linear function over number of updates to schedule the '
                                 'weight with the format: w0:step0,w1:step1,...')
        parser.add_argument('--lambda-denoising-config', default="0.0", type=str, metavar='CONFIG',
                            help='Cross-entropy reconstruction coefficient (denoising autoencoding)'
                                 'use fixed weight during training if set to floating point number. '
                                 'use piecewise linear function over number of updates to schedule the '
                                 'weight with the format: w0:step0,w1:step1,...')
        parser.add_argument('--lambda-otf-bt-config', default="0.0", type=str, metavar='CONFIG',
                            help='cross-entropy reconstruction coefficient (on-the-fly back-translation parallel data)'
                                 'use fixed weight during training if set to floating point number. '
                                 'use piecewise linear function over number of updates to schedule the '
                                 'weight with the format: w0:step0,w1:step1,...')
        parser.add_argument('--lambda-otf-parallel-config', default="0.0", type=str, metavar='CONFIG',
                            help='cross-entropy reconstruction coefficient (on-the-fly back-translation parallel data)'
                                 'use fixed weight during training if set to floating point number. '
                                 'use piecewise linear function over number of updates to schedule the '
                                 'weight with the format: w0:step0,w1:step1,...')

        parser.add_argument('--bt-max-len-a', default=1.1, type=float, metavar='N',
                            help='generate back-translated sequences of maximum length ax + b, where x is the '
                                 'source length')
        parser.add_argument('--bt-max-len-b', default=10.0, type=float, metavar='N',
                            help='generate back-translated sequences of maximum length ax + b, where x is the '
                                 'source length')
        parser.add_argument('--bt-beam-size', default=1, type=int, metavar='N',
                            help='beam size used in beam search of online back-translation')
        parser.add_argument('--max-word-shuffle-distance', default=3.0, type=float, metavar='N',
                            help='maximum word shuffle distance for denoising autoencoding data generation')
        parser.add_argument('--word-dropout-prob', default=0.1, type=float, metavar='N',
                            help='word dropout probability for denoising autoencoding data generation')
        parser.add_argument('--word-blanking-prob', default=0.2, type=float, metavar='N',
                            help='word blanking probability for denoising autoencoding data generation')
        # fmt: on
        parser.add_argument('--bt_dds', action='store_true')
        parser.add_argument('--noise_bt_dds', action='store_true')
        parser.add_argument('--bt_parallel_update', default=0., type=float)
        parser.add_argument('--discount_baseline_size', default=0, type=int)
        
        parser.add_argument('--bt-optimizer', default="SGD", type=str, help="[SGD|ASGD]")
        parser.add_argument('--bt-optimizer-nesterov', action='store_true')
        parser.add_argument('--bt-optimizer-momentum', default=0., type=float)

        parser.add_argument('--data-lr-scheduler', default=None, type=str)
        parser.add_argument('--data-force-anneal', type=int, metavar='N',
                            help='force annealing at specified epoch')
        parser.add_argument('--data-warmup-updates', default=0, type=int, metavar='N',
                            help='warmup the learning rate linearly for the first N updates')
        parser.add_argument('--data-end-learning-rate', default=0.0, type=float)
        parser.add_argument('--data-power', default=1.0, type=float)
        parser.add_argument('--data-total-num-update', default=1000000, type=int)


    def __init__(self, args, dicts, training):
        super().__init__(args, dicts, training)
        self.lambda_parallel, self.lambda_parallel_steps = parse_lambda_config(args.lambda_parallel_config)
        self.lambda_otf_bt, self.lambda_otf_bt_steps = parse_lambda_config(args.lambda_otf_bt_config)
        self.lambda_denoising, self.lambda_denoising_steps = parse_lambda_config(args.lambda_denoising_config)
        self.lambda_otf_parallel, self.lambda_otf_parallel_steps = parse_lambda_config(args.lambda_otf_parallel_config)
        self.backtranslate_datasets = {}
        self.backtranslators = {}
        self.cuda = torch.cuda.is_available() and not args.cpu
        self.baseline = None

    @classmethod
    def setup_task(cls, args, **kwargs):
        dicts, training = MultilingualTranslationTask.prepare(args, **kwargs)
        return cls(args, dicts, training)

    def load_dataset(self, split, epoch=0, **kwargs):
        """Load a dataset split."""

        paths = self.args.data.split(':')
        assert len(paths) > 0
        data_path = paths[epoch % len(paths)]

        def split_exists(split, src, tgt, lang):
            if src is not None:
                filename = os.path.join(data_path, '{}.{}-{}.{}'.format(split, src, tgt, lang))
            else:
                filename = os.path.join(data_path, '{}.{}-None.{}'.format(split, src, tgt))
            if self.args.raw_text and IndexedRawTextDataset.exists(filename):
                return True
            elif not self.args.raw_text and IndexedDataset.exists(filename):
                return True
            return False
        if split == 'train' and self.args.bt_parallel_update > 0:
            lang_pairs = []
            copied_lang_pairs = [p  for p in self.lang_pairs]
            for lang_pair in copied_lang_pairs:
                src, tgt = lang_pair.split('-')
                key = '{}-{}'.format(tgt, src)
                lang_pairs.append(key) 
                lang_pairs.append(lang_pair) 
        else:
            lang_pairs = self.lang_pairs

        # load parallel datasets
        src_datasets, tgt_datasets = {}, {}
        for lang_pair in lang_pairs:
            src, tgt = lang_pair.split('-')
            if split_exists(split, src, tgt, src):
                prefix = os.path.join(data_path, '{}.{}-{}.'.format(split, src, tgt))
            elif split_exists(split, tgt, src, src):
                prefix = os.path.join(data_path, '{}.{}-{}.'.format(split, tgt, src))
            else:
                continue


            src_datasets[lang_pair] = data_utils.load_indexed_dataset(prefix + src, self.dicts[src])
            tgt_datasets[lang_pair] = data_utils.load_indexed_dataset(prefix + tgt, self.dicts[tgt])
            print('| parallel-{} {} {} examples'.format(data_path, split, len(src_datasets[lang_pair])))
        if len(src_datasets) == 0:
            raise FileNotFoundError('Dataset not found: {} ({})'.format(split, data_path))

        def language_pair_dataset(lang_pair):
            src, tgt = lang_pair.split('-')
            src_dataset, tgt_dataset = src_datasets[lang_pair], tgt_datasets[lang_pair]
            return self.alter_dataset_langtok(
                LanguagePairDataset(
                    src_dataset, src_dataset.sizes, self.dicts[src],
                    tgt_dataset, tgt_dataset.sizes, self.dicts[tgt],
                    left_pad_source=self.args.left_pad_source,
                    left_pad_target=self.args.left_pad_target,
                    max_source_positions=self.args.max_source_positions,
                    max_target_positions=self.args.max_target_positions,
                ),
                self.dicts[src].eos(),
                src,
                self.dicts[tgt].eos(),
                tgt,
            )

        # back translation datasets
        backtranslate_datasets = {}
        if split.startswith("train"):
            for lang_pair in self.lang_pairs:
                src, tgt = lang_pair.split('-')
                if not split_exists(split, tgt, None, tgt):
                    raise FileNotFoundError('Dataset not found: backtranslation {} ({})'.format(split, data_path))
                filename = os.path.join(data_path, '{}.{}-None.{}'.format(split, tgt, tgt))
                dataset = data_utils.load_indexed_dataset(filename, self.dicts[tgt])
                lang_pair_dataset_tgt = LanguagePairDataset(
                    dataset,
                    dataset.sizes,
                    self.dicts[tgt],
                    left_pad_source=self.args.left_pad_source,
                    left_pad_target=self.args.left_pad_target,
                )
                #lang_pair_dataset = LanguagePairDataset(
                #    dataset,
                #    dataset.sizes,
                #    src_dict=self.dicts[src],
                #    tgt=dataset,
                #    tgt_sizes=dataset.sizes,
                #    tgt_dict=self.dicts[tgt],
                #    left_pad_source=self.args.left_pad_source,
                #    left_pad_target=self.args.left_pad_target,
                #)
                backtranslate_datasets[lang_pair] = BacktranslationDataset(
                    tgt_dataset=self.alter_dataset_langtok(
                        lang_pair_dataset_tgt,
                        src_eos=self.dicts[tgt].eos(),
                        src_lang=tgt,
                        tgt_lang=src,
                    ),
                    backtranslation_fn=self.backtranslators[lang_pair],
                    src_dict=self.dicts[src], tgt_dict=self.dicts[tgt],
                    output_collater=language_pair_dataset(lang_pair).collater,
                    noising=self.args.noise_bt_dds,
                )
                print('| backtranslate-{}: {} {} {} examples'.format(
                    tgt, data_path, split, len(backtranslate_datasets[lang_pair]),
                ))
                self.backtranslate_datasets[lang_pair] = backtranslate_datasets[lang_pair]


        if split == 'train':
            self.datasets[split] = RoundRobinZipDatasets(
                OrderedDict([
                    (lang_pair, language_pair_dataset(lang_pair))
                    for lang_pair in src_datasets.keys()
                ] + [
                    (_get_bt_dataset_key(lang_pair), dataset)
                    for lang_pair, dataset in backtranslate_datasets.items()
                ]),
                eval_key=None if self.training else "%s-%s" % (self.args.source_lang, self.args.target_lang),
                upsample_factor=self.args.upsample_factor,
            )
        else:
            self.datasets[split] = RoundRobinZipDatasets(
                OrderedDict([
                    (lang_pair, language_pair_dataset(lang_pair))
                    for lang_pair in src_datasets.keys()
                ]),
                eval_key=None if self.training else "%s-%s" % (self.args.source_lang, self.args.target_lang),
            )

        if split == 'valid' and self.args.bt_dds:
            if self.args.max_tokens_valid is not None:
                max_tokens_valid = self.args.max_tokens_valid/4
            else:
                max_tokens_valid = None
            if self.args.max_sentences_valid is not None:
                max_sentences_valid = self.args.max_sentences_valid/4
            else:
                max_sentences_valid = None
            self.dev_itr = self.get_batch_iterator(
                dataset=self.dataset('valid'),
                max_tokens=max_tokens_valid,
                max_sentences=max_sentences_valid,
                max_positions=utils.resolve_max_positions(
                    self.max_positions(),
                ),
                ignore_invalid_inputs=self.args.skip_invalid_size_inputs_valid_test,
                required_batch_size_multiple=self.args.required_batch_size_multiple,
                seed=self.args.seed,
                num_shards=self.args.distributed_world_size,
                shard_id=self.args.distributed_rank,
                num_workers=self.args.num_workers,
                noskip=True,
            )[0]
            self.dev_itr.next_epoch_itr(shuffle=True)

    def build_model(self, args):
        from fairseq import models
        copied_lang_pairs = [p  for p in self.lang_pairs]
        for lang_pair in copied_lang_pairs:
            src, tgt = lang_pair.split('-')
            key = '{}-{}'.format(tgt, src)
            self.lang_pairs.append(key) 
        model = models.build_model(args, self)
        self.lang_pairs = copied_lang_pairs
        if not isinstance(model, FairseqMultiModel):
            raise ValueError('SemisupervisedTranslationTask requires a FairseqMultiModel architecture')
        if self.args.bt_dds:
            # set up data actor finetune optimizer
            bt_params = []
            for lang_pair in self.lang_pairs:
                bt_lang_pair = _get_dds_bt_key(lang_pair)
                for p in model.models[bt_lang_pair].parameters():
                    if p.requires_grad: bt_params.append(p)
            if self.args.bt_optimizer == "SGD":
                self.data_optimizer = torch.optim.SGD(bt_params, lr=self.args.data_actor_lr[0], momentum=self.args.bt_optimizer_momentum, nesterov=self.args.bt_optimizer_nesterov)
                #t_optim = self.args.optimizer
                #self.args.optimizer = "data_sgd"
                #self.data_optimizer = build_optimizer(self.args, bt_params)
                #self.args.optimizer = t_optim
            elif self.args.bt_optimizer == "ASGD":
                self.data_optimizer = torch.optim.ASGD(bt_params, lr=self.args.data_actor_lr[0])
            if self.args.data_lr_scheduler is not None:
                print("Building lr scheduler {} for BT model...".format(self.args.data_lr_scheduler))
                t_scheduler = self.args.lr_scheduler
                self.args.lr_scheduler = self.args.data_lr_scheduler
                self.data_lr_scheduler = lr_scheduler.build_lr_scheduler(self.args, self.data_optimizer)
                self.data_lr_scheduler.step_update(0)
                self.args.lr_scheduler = t_scheduler
            else:
                self.data_lr_scheduler = None
            self.step = 0
        # create SequenceGenerator for each model that has backtranslation dependency on it
        self.sequence_generators = {}
        #if (self.lambda_otf_bt > 0.0 or self.lambda_otf_bt_steps is not None) and self.training:
        if self.training:
            for lang_pair in self.lang_pairs:
                src, tgt = lang_pair.split('-')
                key = '{}-{}'.format(tgt, src)
                self.sequence_generators[key] = SequenceGenerator(
                    tgt_dict=self.dicts[src],
                    beam_size=args.bt_beam_size,
                    max_len_a=args.bt_max_len_a,
                    max_len_b=args.bt_max_len_b,
                    sampling=self.args.sampling,
                    sampling_topk=self.args.sampling_topk,
                    temperature=self.args.temperature,
                )
                decoder_lang_tok_idx = self.get_decoder_langtok(src)

                def backtranslate_fn(
                    sample, model=model.models[key],
                    bos_token=decoder_lang_tok_idx,
                    sequence_generator=self.sequence_generators[key],
                ):
                    return sequence_generator.generate(
                        [model],
                        sample,
                        bos_token=bos_token,
                    )
                self.backtranslators[lang_pair] = backtranslate_fn

        return model

    def train_step(self, sample, model, criterion, optimizer, ignore_grad=False, data_score=None):
        model.train()
        agg_loss, agg_sample_size, agg_logging_output = 0., 0., {}

        def forward_backward(model, samples, logging_output_key, weight, val_loss_data=None, loss_copy=False, ignore=False):
            nonlocal agg_loss, agg_sample_size, agg_logging_output
            if samples is None or len(samples) == 0:
                return
            loss, sample_size, logging_output, nll_loss_data, dev_grad_dotprod = criterion(model, samples, val_loss_data=val_loss_data, loss_copy=loss_copy)
            if ignore: 
                return nll_loss_data, dev_grad_dotprod
            if ignore_grad:
                loss *= 0
            else:
                loss *= weight
            optimizer.backward(loss)
            agg_loss += loss.detach().item()
            # TODO make summing of the sample sizes configurable
            agg_sample_size += sample_size
            agg_logging_output[logging_output_key] = logging_output
            return nll_loss_data, dev_grad_dotprod

        #if self.lambda_parallel > 0.0:
        self.lambda_parallel = 1.
        self.lambda_otf_bt = 1.
        for lang_pair in self.lang_pairs:
            if lang_pair in sample:
                forward_backward(model.models[lang_pair], sample[lang_pair], lang_pair, self.lambda_parallel)
 
        #if self.lambda_otf_bt > 0.0:
        for lang_pair in self.lang_pairs:
            sample_key = _get_bt_dataset_key(lang_pair)
            if self.args.bt_dds:
                params = []
                for p in model.models[lang_pair].parameters():
                    if p.requires_grad: 
                        params.append(p)
                params = params[1:]
                if self.dev_itr.end_of_epoch():
                        self.dev_itr.next_epoch_itr(shuffle=True)
                for valid_sample in self.dev_itr._cur_epoch_itr:
                    valid_sample = utils.move_to_cuda(valid_sample) if self.cuda else valid_sample
                    loss, v_sample_size, _, valid_nll_loss, _ = criterion(model.models[lang_pair], valid_sample[lang_pair], loss_copy=True)
                    valid_nll_loss = valid_nll_loss.sum() / v_sample_size
                    with torch.no_grad():
                        valid_grad = torch.autograd.grad(valid_nll_loss, params, only_inputs=True)
                    break
            #print(sample[sample_key][0])
            loss, sample_size, logging_output, nll_loss_data, dev_grad_dotprod = criterion(model.models[lang_pair], sample[sample_key][0], val_loss_data=None, loss_copy=True)
            
            if self.args.bt_dds:
                # B X T
                nll_loss = nll_loss_data.sum(dim=1) / sample_size
                z = torch.ones_like(nll_loss).requires_grad_(True)
                pgrad = torch.autograd.grad(nll_loss, params, grad_outputs=z, create_graph=True, retain_graph=True, only_inputs=True)
                # calculate jvp loss
                jvp_loss = 0
                vg_norm, pg_norm = 0, 0
                for vg, pg in zip(valid_grad, pgrad):
                    jvp_loss += (vg.data*pg).sum()
                    #vg_norm += vg.data.norm(1)
                    #pg_norm += pg.data.norm(1)
                with torch.no_grad():
                    #dev_grad_dotprod = torch.autograd.grad(jvp_loss, z, retain_graph=False)[0]/(vg_norm*pg_norm+1e-10)
                    dev_grad_dotprod = torch.autograd.grad(jvp_loss, z, retain_graph=False, only_inputs=True)[0]
            optimizer.backward(loss)
            agg_loss += loss.detach().item()
            # TODO make summing of the sample sizes configurable
            agg_sample_size += sample_size
            agg_logging_output[sample_key] = logging_output

            if self.args.bt_dds and self.lambda_denoising > 0:
                # update the bt model using dev grad reward
                bt_lang_pair = _get_dds_bt_key(lang_pair)
                print("lambda_denoising={}".format(self.lambda_denoising))
                #src, tgt = lang_pair.split("-")
                #bt_lang_pair = tgt + "-" + src
                reward = dev_grad_dotprod.view(-1, 1)
                if self.args.baseline and self.args.discount_baseline_size == 0:
                    if self.baseline is None:
                        #self.baseline = (reward.sum()/reward.size()[0]).item()
                        self.baseline = 0
                    else:
                        self.baseline = self.baseline - 0.001 * (self.baseline - (reward.sum()/reward.size()[0]).item())
                    reward = reward - self.baseline
                reward = (reward*self.args.reward_scale).data

                if self.args.discount_reward > 0:
                    B, T = sample[sample_key][1]['target'].size()
                    discount = [1]
                    for i in range(1, T):
                        discount.append(discount[-1] * self.args.discount_reward)
                    discount.reverse()
                       
                    discount = torch.FloatTensor([discount])
                    if reward.is_cuda:
                        discount = discount.cuda()
                    reward = reward.repeat(1, T) * discount
                    if self.args.discount_baseline_size > 0:
                       if not hasattr(self, 'discount_baseline'):
                           self.discount_baseline = [0. for _ in range(self.args.discount_baseline_size)]
                       for i in range(T):
                           self.discount_baseline[i] = self.discount_baseline[i] - 0.001 * (self.discount_baseline[i]-reward[:,i].sum().item()/B)
                           reward[:,i] = reward[:,i] - self.discount_baseline[i]

                loss, _, logging_output, val_loss_data, _ = criterion(model.models[bt_lang_pair], sample[sample_key][1], data_score=reward, loss_copy=False)
                loss = loss * self.lambda_denoising
                #loss = loss * 0
                loss.backward()
                agg_logging_output[bt_lang_pair] = logging_output
                if self.lambda_otf_parallel > 0:
                    print("lambda_denoising={}".format(self.lambda_otf_parallel))
                    loss, _, logging_output, val_loss_data, _ = criterion(model.models[bt_lang_pair], sample[bt_lang_pair], data_score=reward, loss_copy=False)
                    loss = loss * self.args.lambda_otf_parallel
                    loss.backward()
                   
        return agg_loss, agg_sample_size, agg_logging_output, None, None


    def train_step_approx(self, sample, model, criterion, optimizer, ignore_grad=False, data_score=None):
        model.train()
        agg_loss, agg_sample_size, agg_logging_output = 0., 0., {}

        def forward_backward(model, samples, logging_output_key, weight, val_loss_data=None, loss_copy=False, ignore=False):
            nonlocal agg_loss, agg_sample_size, agg_logging_output
            if samples is None or len(samples) == 0:
                return
            loss, sample_size, logging_output, nll_loss_data, dev_grad_dotprod = criterion(model, samples, val_loss_data=val_loss_data, loss_copy=loss_copy)
            if ignore: 
                return nll_loss_data, dev_grad_dotprod
            if ignore_grad:
                loss *= 0
            else:
                loss *= weight
            optimizer.backward(loss)
            agg_loss += loss.detach().item()
            # TODO make summing of the sample sizes configurable
            agg_sample_size += sample_size
            agg_logging_output[logging_output_key] = logging_output
            return nll_loss_data, dev_grad_dotprod

        #if self.lambda_parallel > 0.0:
        self.lambda_parallel = 1.
        self.lambda_otf_bt = 1.
        for lang_pair in self.lang_pairs:
            forward_backward(model.models[lang_pair], sample[lang_pair], lang_pair, self.lambda_parallel)
 
        #if self.lambda_otf_bt > 0.0:
        for lang_pair in self.lang_pairs:
            sample_key = _get_bt_dataset_key(lang_pair)
            if self.args.bt_dds:
                optimizer.clone_param()
                optimizer.save_cur_train_grad()
                if self.dev_itr.end_of_epoch():
                        self.dev_itr.next_epoch_itr(shuffle=True)
                for valid_sample in self.dev_itr._cur_epoch_itr:
                    valid_sample = utils.move_to_cuda(valid_sample) if self.cuda else valid_sample
                    loss, v_sample_size, _, _, _ = criterion(model.models[lang_pair], valid_sample[lang_pair])
                    optimizer.backward(loss)
                    break
                optimizer.save_new_grad(v_sample_size)
                # get per example reward
                eta = 1e-7
                optimizer.add_new_grad(eta=eta)
                with torch.no_grad():
                    loss, _, _, val_loss_data, _ = criterion(model.models[lang_pair], sample[sample_key][0], loss_copy=True)
                optimizer.switch_param(clear_cache=True)
                optimizer.reset_cur_train_grad()
            else:
                val_loss_data = None
            #print(sample[sample_key][0])
            nll_loss_data, dev_grad_dotprod = forward_backward(model.models[lang_pair], sample[sample_key][0], sample_key, self.lambda_otf_bt, val_loss_data=val_loss_data, loss_copy=True)
            
            if self.args.bt_dds and self.lambda_denoising > 0:
                # update the bt model using dev grad reward
                bt_lang_pair = _get_dds_bt_key(lang_pair)
                #src, tgt = lang_pair.split("-")
                #bt_lang_pair = tgt + "-" + src
                reward = dev_grad_dotprod / eta
                if self.args.baseline:
                    if self.baseline is None:
                        #self.baseline = (reward.sum()/reward.size()[0]).item()
                        self.baseline = 0
                    else:
                        self.baseline = self.baseline - 0.001 * (self.baseline - (reward.sum()/reward.size()[0]).item())
                    reward = reward - self.baseline
                #reward = reward * 0.0001
                reward = reward * self.args.reward_scale

                loss, _, logging_output, val_loss_data, _ = criterion(model.models[bt_lang_pair], sample[sample_key][1], data_score=reward, loss_copy=False)
                loss = loss * self.lambda_denoising
                #loss = loss * 0
                loss.backward()
                self.data_optimizer.step()
                self.data_optimizer.zero_grad()
                #print(self.lambda_denoising)
                agg_logging_output[bt_lang_pair] = logging_output
                   
        return agg_loss, agg_sample_size, agg_logging_output, None, None

    #def valid_step(self, sample, model, criterion):
    #    model.eval()
    #    with torch.no_grad():
    #        agg_loss, agg_sample_size, agg_logging_output = 0., 0., {}
    #        for lang_pair in self.eval_lang_pairs:
    #            if lang_pair not in sample or sample[lang_pair][0] is None or len(sample[lang_pair][0]) == 0:
    #                continue
    #            loss, sample_size, logging_output, _, _ = criterion(model.models[lang_pair], sample[lang_pair][0])
    #            agg_loss += loss.data.item()
    #            # TODO make summing of the sample sizes configurable
    #            agg_sample_size += sample_size
    #            agg_logging_output[lang_pair] = logging_output
    #    return agg_loss, agg_sample_size, agg_logging_output


    def update_step(self, num_updates):
        def lambda_step_func(config, n_iter):
            """
            Update a lambda value according to its schedule configuration.
            """
            ranges = [i for i in range(len(config) - 1) if config[i][0] <= n_iter < config[i + 1][0]]
            if len(ranges) == 0:
                assert n_iter >= config[-1][0]
                return config[-1][1]
            assert len(ranges) == 1
            i = ranges[0]
            x_a, y_a = config[i]
            x_b, y_b = config[i + 1]
            return y_a + (n_iter - x_a) * float(y_b - y_a) / float(x_b - x_a)
        if self.args.bt_dds:
            self.data_optimizer.step()
            self.data_optimizer.zero_grad()
            self.step += 1
            if self.data_lr_scheduler is not None:
                lr = self.data_lr_scheduler.step_update(self.step)
                print("BT lr={}".format(lr))
        if self.lambda_otf_parallel_steps is not None:
            self.lambda_otf_parallel = lambda_step_func(self.lambda_otf_parallel_steps, self.step)

        if self.lambda_parallel_steps is not None:
            self.lambda_parallel = lambda_step_func(self.lambda_parallel_steps, self.step)
        if self.lambda_otf_bt_steps is not None:
            self.lambda_otf_bt = lambda_step_func(self.lambda_otf_bt_steps, self.step)
        if self.lambda_denoising_steps is not None:
            self.lambda_denoising = lambda_step_func(self.lambda_denoising_steps, self.step)

    def aggregate_logging_outputs(self, logging_outputs, criterion):
        # aggregate logging outputs for each language pair
        logging_output_keys = {
            key
            for logging_output in logging_outputs
            for key in logging_output
        }
        lang_pair_keys = set(self.lang_pairs + [
            _get_bt_dataset_key(lang_pair)
            for lang_pair in self.lang_pairs
        ] + [
            _get_denoising_dataset_key(lang_pair)
            for lang_pair in self.lang_pairs
        ] + [_get_dds_bt_key(lang_pair) for lang_pair in self.lang_pairs])
        logging_output_keys = logging_output_keys.intersection(lang_pair_keys)
        return super().aggregate_logging_outputs(logging_outputs, criterion, logging_output_keys)
