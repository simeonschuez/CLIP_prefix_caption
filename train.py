import torch
from torch.nn import functional as nnf
from torch.utils.data import DataLoader
from transformers import AdamW, get_linear_schedule_with_warmup
from tqdm import tqdm
import os
import sys
from data_utils.refcoco import RefCocoDataset, build_dataset
from data_utils.transformations import SquarePad, CoverWithNoise, update_transforms
from model import ClipREGModel, ClipREGPrefix, ClipNoContextREGModel, ClipNoContextREGPrefix, MappingType
from os.path import join, dirname, abspath
from configuration import Config
import json
from generate_utils import generate_greedy
from collections import defaultdict
import argparse

file_path = dirname(abspath(__file__))
module_path = join(file_path, "nlgeval")
sys.path.append(module_path)
from nlgeval import NLGEval


def normalize_with_tokenizer(sent, tokenizer):
    """
    use tokenizer to normalize annotated captions
    (corresponding to system output)
    """

    return tokenizer.decode(tokenizer.encode(sent), skip_special_tokens=True)


def train(
    args,
    config,
    train_dataset: RefCocoDataset,
    val_dataset: RefCocoDataset,
    ciderval_dataset: RefCocoDataset,
    model: ClipREGModel,
    lr: float = 2e-5,
    warmup_steps: int = 5000,
    output_dir: str = ".",
    output_prefix: str = "",
    save_samples=True,
    metrics_to_omit=["SPICE"],
    early_stopping=True,
    early_stopping_scope=8,
    device="cuda" if torch.cuda.is_available() else "cpu",
):
    print(f"train model on device {device}")

    batch_size = config.batch_size
    epochs = config.epochs
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    model = model.to(device)
    optimizer = AdamW(model.parameters(), lr=lr)
    train_dataloader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, drop_last=True
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=warmup_steps,
        num_training_steps=epochs * len(train_dataloader),
    )

    val_dataloader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=True, drop_last=True
    )
    ciderval_dataloader = DataLoader(
        ciderval_dataset, batch_size=1, shuffle=False, drop_last=False
    )

    evaluator = NLGEval(
        no_skipthoughts=True, no_glove=True, metrics_to_omit=metrics_to_omit
    )

    cider_scores = []

    for epoch in range(epochs):
        print(f">>> Training epoch {epoch}")
        sys.stdout.flush()

        model.train()
        train_progress = tqdm(total=len(train_dataloader), desc=output_prefix)
        losses = []
        for idx, (ann_id, *encoder_input, tokens, mask) in enumerate(train_dataloader):
            model.zero_grad()
            target, context, loc = encoder_input
            tokens, mask, target, context, loc = (
                tokens.to(device),
                mask.to(device),
                target.to(device, dtype=torch.float32),
                context.to(device, dtype=torch.float32),
                loc.to(device, dtype=torch.float32),
            )
            outputs = model(tokens, target=target, context=context, loc=loc, mask=mask)
            logits = outputs.logits[:, train_dataset.prefix_length - 1: -1]
            loss = nnf.cross_entropy(
                logits.reshape(-1, logits.shape[-1]), tokens.flatten(), ignore_index=0
            )
            loss.backward()
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            losses.append(loss.item())
            train_progress.set_postfix({"loss": loss.item()})
            train_progress.update()
        train_progress.close()
        train_loss = torch.tensor(losses).mean().item()
        print(f"Train loss: {train_loss}")

        model.eval()
        print(f">>> Loss Evaluation epoch {epoch}")
        val_progress = tqdm(total=len(val_dataloader), desc=output_prefix)
        losses = []
        for idx, (ann_id, *encoder_input, tokens, mask) in enumerate(val_dataloader):
            target, context, loc = encoder_input
            tokens, mask, target, context, loc = (
                tokens.to(device),
                mask.to(device),
                target.to(device, dtype=torch.float32),
                context.to(device, dtype=torch.float32),
                loc.to(device, dtype=torch.float32),
            )
            outputs = model(tokens, target=target, context=context, loc=loc, mask=mask)
            logits = outputs.logits[:, val_dataset.prefix_length - 1: -1]
            loss = nnf.cross_entropy(
                logits.reshape(-1, logits.shape[-1]), tokens.flatten(), ignore_index=0
            )
            losses.append(loss.item())
            val_progress.set_postfix({"loss": loss.item()})
            val_progress.update()
        val_progress.close()
        val_loss = torch.tensor(losses).mean().item()
        print(f"Val loss: {val_loss}")

        print(f">>> CIDEr Evaluation epoch {epoch}")
        # construct reference dict
        annotations = defaultdict(list)
        for a in ciderval_dataset.annot:
            annotations[a[0]].append(a[2])
        _, hypotheses, ids_hypotheses, references = [], [], [], []

        ciderval_progress = tqdm(total=len(ciderval_dataloader), desc=output_prefix)
        for idx, (ann_id, *encoder_input, tokens, mask) in enumerate(
            ciderval_dataloader
        ):
            target, context, loc = encoder_input
            tokens, mask, target, context, loc = (
                tokens.to(device),
                mask.to(device),
                target.to(device, dtype=torch.float32),
                context.to(device, dtype=torch.float32),
                loc.to(device, dtype=torch.float32),
            )

            prefix_embed = model.make_visual_prefix(target=target, context=context, loc=loc).reshape(
                1, ciderval_dataset.prefix_length, -1
            )
            hyp, _, _ = generate_greedy(model, model.tokenizer, embed=prefix_embed)

            hypotheses.append(hyp)
            id_hyp = {"ann_id": ann_id.item(), "expression": hyp}
            ids_hypotheses.append(id_hyp)

            # get annotated references
            refs = [annotations[i] for i in ann_id.tolist()]
            normalized_refs = [
                [normalize_with_tokenizer(r, model.tokenizer) for r in _refs]
                for _refs in refs
            ]
            references += normalized_refs

            ciderval_progress.set_postfix({"generated": ann_id.item()})
            ciderval_progress.update()

        ciderval_progress.close()

        # transpose references to get correct format
        transposed_references = list(map(list, zip(*references)))

        # calculate cider score from hypotheses and references
        metrics_dict = evaluator.compute_metrics(
            ref_list=transposed_references, hyp_list=hypotheses
        )

        cider_score = metrics_dict["CIDEr"]
        cider_scores.append(cider_score)
        print(f"CIDEr score: {cider_score}")

        if save_samples:
            with open(
                os.path.join(output_dir, f"{output_prefix}-{epoch:03d}-samples.json"),
                "w",
            ) as f:
                json.dump(ids_hypotheses, f)

        if epoch % config.save_every == 0 or epoch == epochs - 1:
            checkpoint_name = f"{output_prefix}-{epoch:03d}-noise_{str(args.target_noise).replace('.', '-')}.pt"

            torch.save(
                {
                    "epoch": epoch,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "lr_scheduler_state_dict": scheduler.state_dict(),
                    "train_loss": train_loss,
                    "val_loss": val_loss,
                    "cider_score": cider_score,
                    "config": config,
                },
                os.path.join(output_dir, checkpoint_name),
            )

        if early_stopping:
            if cider_score < min(cider_scores[-early_stopping_scope:]):
                print(
                    f"no improvements within the last {early_stopping_scope} epochs -- early stopping triggered!"
                )
                break

    return model


def main(args, config):

    prefix_length = config.prefix_length
    prefix_dim = 640 if config.is_rn else 512
    config.mapping_type = {
        "mlp": MappingType.MLP,
        "transformer": MappingType.Transformer,
    }[config.mapping_type]

    # select model class 
    # depending on config.only_prefix and args.no_context

    if config.only_prefix:
        print("Train only prefix")
        if args.no_context: 
            model = ClipNoContextREGPrefix(
                prefix_length,
                clip_length=config.prefix_length_clip,
                prefix_size=prefix_dim,
                num_layers=config.num_layers,
                mapping_type=config.mapping_type,
            )
        else:
            model = ClipREGPrefix(
                prefix_length,
                clip_length=config.prefix_length_clip,
                prefix_size=prefix_dim,
                num_layers=config.num_layers,
                mapping_type=config.mapping_type,
            )
    else:
        print("Train both prefix and GPT")
        if args.no_context: 
            model = ClipNoContextREGModel(
                prefix_length,
                clip_length=config.prefix_length_clip,
                prefix_size=prefix_dim,
                num_layers=config.num_layers,
                mapping_type=config.mapping_type,
            )
        else:
            model = ClipREGModel(
                prefix_length,
                clip_length=config.prefix_length_clip,
                prefix_size=prefix_dim,
                num_layers=config.num_layers,
                mapping_type=config.mapping_type,
            )

    print(f"Built {model.__class__.__name__} model")
    sys.stdout.flush()

    # handle transformations
    # depending on args.target_noise

    model_transform = model.backbone.preprocess
    if args.target_noise > 0:
        print(f"apply noise to target image (ratio {args.target_noise})")
        target_transform = update_transforms(
            model_transform,
            pad_transform=SquarePad(),
            noise_transform=CoverWithNoise(args.target_noise),
        )
        context_transform = update_transforms(
            model_transform, pad_transform=SquarePad()
        )
    else:
        print("do not apply noise to target image")
        target_transform = context_transform = update_transforms(
            model_transform, pad_transform=SquarePad()
        )

    transform = {"target": target_transform, "context": context_transform}

    # build datasets

    train_dataset = build_dataset(
        transform=transform,
        tokenizer=model.tokenizer,
        ref_dir=join(config.ref_base, config.dataset),
        coco_dir=config.coco_dir,
        verbose=config.verbose,
        prefix_length=model.prefix_length,
        mode="training",
    )

    val_dataset = build_dataset(
        transform=transform,
        tokenizer=model.tokenizer,
        ref_dir=join(config.ref_base, config.dataset),
        coco_dir=config.coco_dir,
        verbose=config.verbose,
        prefix_length=model.prefix_length,
        mode="val",
    )

    ciderval_dataset = build_dataset(
        transform=transform,
        tokenizer=model.tokenizer,
        ref_dir=join(config.ref_base, config.dataset),
        coco_dir=config.coco_dir,
        verbose=config.verbose,
        prefix_length=model.prefix_length,
        mode="val",
        return_unique=True,
    )

    # run training

    train(
        args,
        config,
        train_dataset,
        val_dataset,
        ciderval_dataset,
        model,
        output_dir=config.checkpoint_dir,
        output_prefix=config.output_prefix,
    )


if __name__ == "__main__":
    config = Config()

    parser = argparse.ArgumentParser()
    parser.add_argument("--target_noise", default=0.0, type=float)
    parser.add_argument("--no_context", action="store_true")
    args = parser.parse_args()

    main(args, config)
