"""
Test script for SAR3D model on images.
This script loads images from a directory, processes them through the SAR3D model,
and generates 3D models with rendered videos.

Key steps:
1. Load and preprocess input images 
2. Extract DINO features
3. Generate triplane representation
4. Render 3D models with camera views
"""

import os
import sys
import time
import torch
from PIL import Image
from transformers import AutoImageProcessor, Dinov2Model

# Import custom modules
import utils.dist as dist
from utils import arg_util, misc
from utils.render_utils import render_video_given_triplane, render_video_given_triplane_mesh

# Import optimized transformer components
from xformers.triton import FusedLayerNorm as LayerNorm
from xformers.components.activations import Activation
from xformers.components.feedforward import fused_mlp


def build_everything(args: arg_util.Args):
    """
    Build and initialize SAR3D model components.
    
    Args:
        args: Command line arguments with model configs
        
    Returns:
        trainer: Initialized SAR3D trainer with loaded weights
    """
    # Load model checkpoints
    ckpt_state = torch.load(args.ar_ckpt_path, map_location='cpu')['trainer']
    
    # Build models
    from models import build_vae_var_3D_VAR
    from trainer import VARTrainer
    from torch.nn.parallel import DistributedDataParallel as DDP

    # Initialize VAE and VAR models
    vae_local, var_wo_ddp = build_vae_var_3D_VAR(
        device=dist.get_device(), patch_nums=args.patch_nums,
        num_classes=1, depth=args.depth, shared_aln=args.saln, 
        attn_l2_norm=args.anorm, flash_if_available=args.fuse,
        fused_if_available=args.fuse, init_adaln=args.aln,
        init_adaln_gamma=args.alng, init_head=args.hd,
        init_std=args.ini, args=args
    )

    # Load VAE weights
    vae_ckpt = args.vqvae_pretrained_path
    if dist.is_local_master() and not os.path.exists(vae_ckpt):
        raise FileNotFoundError(f"VAE checkpoint not found at {vae_ckpt}")
    
    vae_local.load_state_dict(torch.load(vae_ckpt, map_location='cpu'), strict=True)

    # Compile and wrap models
    vae_local = args.compile_model(vae_local, args.vfast)
    var_wo_ddp = args.compile_model(var_wo_ddp, args.tfast)
    var = (DDP if dist.initialized() else NullDDP)(
        var_wo_ddp, 
        device_ids=[dist.get_local_rank()],
        find_unused_parameters=False,
        broadcast_buffers=False
    )

    # Print model info
    print(f'[INIT] VAR model = {var_wo_ddp}\n')
    count_p = lambda m: f'{sum(p.numel() for p in m.parameters())/1e6:.2f}M'
    print(f'[INIT] Parameters:')
    print(f'  VAE: {count_p(vae_local)}')
    print(f'  VAE Encoder: {count_p(vae_local.encoder)}') 
    print(f'  VAE Decoder: {count_p(vae_local.decoder)}')
    print(f'  VAE Quantizer: {count_p(vae_local.decoder.superresolution.quantize)}')
    print(f'  VAR: {count_p(var_wo_ddp)}\n')

    # Initialize trainer
    trainer = VARTrainer(
        device=args.device,
        patch_nums=args.patch_nums,
        resos=args.resos,
        vae_local=vae_local,
        var_wo_ddp=var_wo_ddp,
        var=var,
        var_opt=None,
        label_smooth=args.ls
    )
    trainer.load_state_dict(ckpt_state, strict=False, skip_vae=True)

    # Cleanup
    del vae_local, var_wo_ddp, var
    return trainer


def main_test():
    """
    Main testing pipeline for processing images and generating 3D reconstructions.
    """
    # Initialize
    args = arg_util.init_dist_and_get_args()
    sar3d = build_everything(args)
    
    # Get input images
    png_files = [
        os.path.join(root, f) 
        for root, _, files in os.walk("./test_images")
        for f in files if f.endswith('.png')
    ]
    print(f"Found {len(png_files)} images to process")

    # Process each image
    for png_file in png_files:
        name = os.path.splitext(os.path.basename(png_file))[0]
        save_dir = os.path.join(args.save_path, name, str(int(time.time())))
        os.makedirs(save_dir, exist_ok=True)

        # Load and preprocess image
        img = preprocess_image(png_file, save_dir)
        
        # Extract DINO features
        dino_feats = extract_dino_features(img)
        
        # Generate 3D reconstruction
        with torch.inference_mode():
            triplane, g_BL = generate_triplane(sar3d, dino_feats)
            render_results(args, sar3d, triplane, g_BL, name, save_dir)


def preprocess_image(png_file: str, save_dir: str) -> Image:
    """Helper function to load and preprocess input image"""
    img = Image.open(png_file)
    if img.mode == "RGBA":
        # Handle RGBA by adding white background
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img)
        img = bg.convert('RGB')
    
    img = img.resize((256, 256))
    img.save(os.path.join(save_dir, "input.png"))
    return img


def extract_dino_features(img: Image):
    """Helper function to extract DINO features from image"""
    model_id = "facebook/dinov2-large"
    processor = AutoImageProcessor.from_pretrained(model_id)
    model = Dinov2Model.from_pretrained(model_id)
    
    with torch.no_grad():
        inputs = processor(images=img, return_tensors="pt")
        outputs = model(**inputs)
        return {
            'embeddings': outputs.last_hidden_state[:,1:].to(dist.get_device(), non_blocking=True),
            'pooled': outputs.pooler_output.to(dist.get_device(), non_blocking=True)
        }


def generate_triplane(sar3d, dino_feats):
    """Helper function to generate triplane representation"""
    return sar3d.var_wo_ddp.autoregressive_infer_cfg_3D_VAR_image_l2norm(
        B=1,
        dino_image_embeddings=dino_feats['embeddings'],
        pooler_output=dino_feats['pooled'],
        cfg=4,
        top_k=10,
        top_p=0.5,
        more_smooth=False
    )


def render_results(args, sar3d, triplane, g_BL, name, save_dir):
    """Helper function to render 3D reconstruction results"""
    # Load and transform camera parameters
    camera = torch.load("./camera.pt").cpu()
    rot = get_camera_rotation(args.flexicubes)
    camera = transform_camera(camera, rot)

    # Render each triplane
    for i, tri in enumerate(triplane):
        render_fn = render_video_given_triplane_mesh if args.flexicubes else render_video_given_triplane
        
        render_fn(
            tri.unsqueeze(0),
            sar3d.vae_local,
            name_prefix=name,
            render_reference={'c': camera},
            save_img=True,
            save_mesh=True,
            save_path=save_dir
        )

        if args.save_BL:
            torch.save(g_BL.detach().cpu(), os.path.join(save_dir, "G_BL.pt"))


def get_camera_rotation(use_flexicubes):
    """Helper function to get camera rotation matrix"""
    if use_flexicubes:
        return torch.tensor([
            [1, 0, 0, 0],
            [0, 1, 0, 0], 
            [0, 0, -1, 0],
            [0, 0, 0, 1]
        ], dtype=torch.float32)
    else:
        return torch.tensor([
            [1, 0, 0, 0],
            [0, -1, 0, 0],
            [0, 0, -1, 0],
            [0, 0, 0, -1]
        ], dtype=torch.float32)


def transform_camera(camera, rot):
    """Helper function to transform camera parameters"""
    camera = camera.clone()
    for i in range(camera.shape[0]):
        camera[i, :16] = (rot @ camera[i, :16].reshape(4,4)).reshape(-1)
    return camera


class NullDDP(torch.nn.Module):
    """Dummy DistributedDataParallel wrapper for single-GPU training"""
    def __init__(self, module, *args, **kwargs):
        super().__init__()
        self.module = module
    
    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)


if __name__ == '__main__':
    try:
        main_test()
    finally:
        # Cleanup
        dist.finalize()
        if isinstance(sys.stdout, misc.SyncPrint) and isinstance(sys.stderr, misc.SyncPrint):
            sys.stdout.close()
            sys.stderr.close()
