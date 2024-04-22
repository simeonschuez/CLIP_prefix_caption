from pathlib import Path
import argparse
import torch
import re
from tqdm import tqdm
import os
import os.path as osp
from configuration import Config
from data_utils.transformations import SquarePad, CoverWithNoise, update_transforms
from model import (
    ClipREGModel,
    ClipREGPrefix,
    ClipNoContextREGModel,
    ClipNoContextREGPrefix
)
from data_utils import refcoco, paco
from generate_utils import generate_beam, generate_greedy, generate_topp
import json


def main(args, local_config):
    device = "cuda" if torch.cuda.is_available() else "cpu"

    checkpoint_data = torch.load(args.model_checkpoint, map_location="cpu")

    config = checkpoint_data['config']
    # paths from local config
    config.coco_dir = local_config.coco_dir
    config.ref_base = local_config.ref_base
    config.ref_dir = local_config.ref_dir
    
    model_args = checkpoint_data['args']
    model_epoch = checkpoint_data['epoch']
    
    # extract info
    
    dataset_str = config.dataset

    architecture_str = 'clpgpt'

    if model_args.no_context:
        context = 'nocontext'
    else:
        context = 'global'
    context_str = f'context:{context}'

    noise_str = f"noise:{str(model_args.target_noise).replace('.', '-')}"

    epoch_str = f"epoch:{str(model_epoch).rjust(2,'0')}"
    
    # create output dir
    if args.auto_checkpoint_path:
        outdir = osp.join(args.out_dir, 'models', dataset_str, f'{noise_str.replace(":", "_")}_{context}')
    else: 
        outdir = args.out_dir
    
    if not osp.isdir(outdir):
        print(f"create output directory {outdir}")
        os.makedirs(outdir)
                

    # make model
    prefix_length = config.prefix_length
    prefix_dim = 640 if config.is_rn else 512
    
    # parse checkpoint path    
    checkpoint_name = osp.split(args.model_checkpoint)[-1]
    print(f'checkpoint name: {checkpoint_name}')
        
    checkpoint_noise = re.search(r'noise_(\d\-\d+)', checkpoint_name)
    assert checkpoint_noise is not None, 'could not extract noise information from checkpoint filename'
    target_noise = float(checkpoint_noise.group(1).replace('-', '.'))
    
    only_prefix = '_prefix' in checkpoint_name
    print('using {p} model {c} context, noise: {n}'.format(
        p='prefix' if only_prefix else 'full',
        c='without' if model_args.no_context else 'with',
        n=target_noise
    ))
    
    if only_prefix:
        if model_args.no_context:
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
        if model_args.no_context:
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

    model.load_state_dict(checkpoint_data["model_state_dict"])
    print('successfully loaded weights')
    model.to(device)
    model.eval()
    
    model_transform = model.backbone.preprocess
    if target_noise > 0:
        print(f"apply noise to target image (ratio {target_noise})")
        target_transform = update_transforms(
            model_transform,
            pad_transform=SquarePad(),
            noise_transform=CoverWithNoise(target_noise),
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
    if config.dataset.lower() == 'paco':
        build_dataset = paco.build_dataset
        ann_dir = config.paco_base
        img_dir = config.paco_imgs
    else:
        build_dataset = refcoco.build_dataset
        ann_dir = osp.join(config.ref_base, config.dataset)
        img_dir = config.coco_dir
        
    # make dataset
    dataset = build_dataset(
        transform=transform,
        tokenizer=model.tokenizer,
        ann_dir=ann_dir,
        img_dir=img_dir,
        verbose=config.verbose,
        prefix_length=model.prefix_length,
        return_unique=True,
        mode=args.split,
    )
    data_iter = iter(dataset)

    if args.decoding_method == "beam":
        print("using beam search")

        def generate(model, tokenizer, embed):
            return generate_beam(model, tokenizer, embed=embed)[0][0]

    elif args.decoding_method == "topp":
        print("using top-p decoding")

        def generate(model, tokenizer, embed):
            return generate_topp(model, tokenizer, embed=embed)[0][0]

    else:
        print("using greedy search")

        def generate(model, tokenizer, embed):
            return generate_greedy(model, tokenizer, embed=embed)[0]

    results = []

    for ann_id, *encoder_input, _, _ in tqdm(data_iter, total=len(dataset)):

        target, context, loc = encoder_input
        target, context, loc = (
            target.to(device, dtype=torch.float32).unsqueeze(0),
            context.to(device, dtype=torch.float32).unsqueeze(0),
            loc.to(device, dtype=torch.float32).unsqueeze(0),
        )

        prefix_embed = model.make_visual_prefix(
            target=target, context=context, loc=loc
        ).reshape(1, model.prefix_length, -1)

        generated = generate(model, model.tokenizer, prefix_embed)

        results.append({"ann_id": ann_id, "generated": generated})


    file_prefix = f'{dataset_str}_{args.split}_{architecture_str}_{context_str}_{noise_str}_{epoch_str}'
    if args.decoding_method != 'greedy': 
        file_prefix += f'_{args.decoding_method}'
    
    out_file = osp.join(
        outdir,
        file_prefix + '_generated.json'
    )
     
    with open(out_file, "w") as f:
        print(f"write results to {out_file}")
        json.dump(results, f)


if __name__ == "__main__":
    config = Config()

    parser = argparse.ArgumentParser()

    parser.add_argument("--model_checkpoint", required=True)
    parser.add_argument(
        "--decoding_method",
        default="greedy",
        choices=["greedy", "beam", "topp"],
        type=str.lower,
    )
    parser.add_argument("--out_dir", default="./generated")
    parser.add_argument(
        "--split", default="val", choices=["val", "testa", "testb"], type=str.lower
    )    
    parser.add_argument("--auto_checkpoint_path", default=True, type=bool)
    args = parser.parse_args()

    # make sure the checkpoint exists
    assert (
        Path(args.model_checkpoint).expanduser().exists()
    ), f"checkpoint {args.model_checkpoint} does not exist"

    main(args, config)
