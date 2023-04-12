from typing import Tuple

import os
import shlex

import datasets
import torch
import transformers
from transformers import AutoTokenizer, HfArgumentParser, set_seed
from transformers.trainer_utils import get_last_checkpoint

from collator import CustomCollator
from data_helpers import load_dpr_corpus, NQ_DEV, NQ_TRAIN
from models import load_encoder_decoder, load_embedder_and_tokenizer, InversionModel
from run_args import ModelArguments, DataTrainingArguments, TrainingArguments
from tokenize_data import tokenize_function
from trainer import InversionTrainer


transformers.logging.set_verbosity_error()

#############################################################################

WANDB_ARGS_STR = '--per_device_train_batch_size 128 --per_device_eval_batch_size 128 --max_seq_length 128 --model_name_or_path t5-base --embedder_model_name gtr_base --num_repeat_tokens 16 --embedder_no_grad True --exp_group_name mar17-baselines --learning_rate 0.0003 --freeze_strategy none --embedder_fake_with_zeros False --use_frozen_embeddings_as_input False --num_train_epochs 24 --max_eval_samples 500 --eval_steps 25000 --warmup_steps 100000 --bf16=1 --use_wandb=1'
args = shlex.split(WANDB_ARGS_STR)

def load_inversion_model_and_trainer(checkpoint_folder: str) -> Tuple[InversionModel, InversionTrainer]:
    checkpoint = get_last_checkpoint(checkpoint_folder) # a checkpoint
    print("[0] Loading model from checkpoint:", checkpoint)
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses(args=args)

    training_args.dataloader_num_workers = 0 # no multiprocesisng :)

    checkpoint = '/home/jxm3/research/retrieval/inversion/saves/c9a30cba01655d513e46040f949f6da7'
    training_args = torch.load(os.path.join(checkpoint, 'training_args.bin'))
    training_args.use_wandb = False

    set_seed(training_args.seed)

    #############################################################################
    print("[1] creating model & stuff")
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        padding=True,
        truncation='max_length',
        max_length=model_args.max_seq_length,
    )
    embedder, embedder_tokenizer = load_embedder_and_tokenizer(
        name=model_args.embedder_model_name
    )
    model = InversionModel(
        embedder=embedder,
        embedder_tokenizer=embedder_tokenizer,
        tokenizer=tokenizer,
        encoder_decoder=load_encoder_decoder(
            model_name=model_args.model_name_or_path
        ),
        num_repeat_tokens=model_args.num_repeat_tokens,
        embedder_no_grad=model_args.embedder_no_grad,
        embedder_fake_with_zeros=model_args.embedder_fake_with_zeros,
        use_frozen_embeddings_as_input=model_args.use_frozen_embeddings_as_input,
        use_embedding_batch_norm=model_args.use_embedding_batch_norm,
        encoder_dropout_disabled=model_args.encoder_dropout_disabled,
        decoder_dropout_disabled=model_args.decoder_dropout_disabled,
        freeze_strategy=model_args.freeze_strategy,
        token_decode_alpha=model_args.token_decode_alpha,
    )
    model._keys_to_ignore_on_save = []

    #############################################################################

    text_column_name = "text"

    raw_datasets = datasets.DatasetDict({
        "train": load_dpr_corpus(NQ_TRAIN),
        "validation": load_dpr_corpus(NQ_DEV),
    })
    column_names = list(raw_datasets["train"].features)

    print("[2] tokenizing dataset")
    tokenized_datasets = raw_datasets.map(
        tokenize_function(tokenizer, embedder_tokenizer, text_column_name, model_args.max_seq_length),
        batched=True,
        remove_columns=column_names,
        load_from_cache_file=not data_args.overwrite_cache,
        desc="Running tokenizer on dataset",
    )
    train_dataset = tokenized_datasets["train"]
    eval_dataset = tokenized_datasets["validation"]

    if data_args.max_eval_samples is not None:
        max_eval_samples = min(len(eval_dataset), data_args.max_eval_samples)
        eval_dataset = eval_dataset.select(range(max_eval_samples))


    #############################################################################

    # Initialize our Trainer
    print("[3] initializing trainer")
    trainer = InversionTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
        data_collator=CustomCollator(tokenizer=tokenizer),
    )

    print("[4] getting ckpnt")
    # *** Evaluation ***

    print("[5] loading ckpt")
    trainer._load_from_checkpoint(checkpoint)
    return trainer
