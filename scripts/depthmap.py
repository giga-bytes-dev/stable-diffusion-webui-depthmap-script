# Author: thygate
# https://github.com/thygate/stable-diffusion-webui-depthmap-script

import modules
import modules.scripts as scripts
import gradio as gr

from modules.call_queue import wrap_gradio_gpu_call, wrap_queued_call, wrap_gradio_call
from modules.ui import plaintext_to_html
from modules import processing, images, shared, sd_samplers, devices
from modules.processing import create_infotext, process_images, Processed
from modules.shared import opts, cmd_opts, state, Options
from modules import script_callbacks
from numba import njit, prange
from torchvision.transforms import Compose, transforms
from PIL import Image
from pathlib import Path
from operator import getitem
from tqdm import trange
from functools import reduce

import sys
import torch, gc
import torch.nn as nn
import cv2
import os.path
import contextlib
import matplotlib.pyplot as plt
import numpy as np
import skimage.measure
import argparse

sys.path.append('extensions/stable-diffusion-webui-depthmap-script/scripts')

# midas imports
from midas.dpt_depth import DPTDepthModel
from midas.midas_net import MidasNet
from midas.midas_net_custom import MidasNet_small
from midas.transforms import Resize, NormalizeImage, PrepareForNet

# AdelaiDepth/LeReS imports
from lib.multi_depth_model_woauxi import RelDepthModel
from lib.net_tools import strip_prefix_if_present

# pix2pix/merge net imports
from pix2pix.options.test_options import TestOptions
from pix2pix.models.pix2pix4depth_model import Pix2Pix4DepthModel
from pix2pix.util import util
import pix2pix.models
import pix2pix.data


whole_size_threshold = 1600  # R_max from the paper
pix2pixsize = 1024
scriptname = "DepthMap v0.3.3"

class Script(scripts.Script):
	def title(self):
		return scriptname

	def show(self, is_img2img):
		return True

	def ui(self, is_img2img):
		with gr.Column(variant='panel'):
			with gr.Row():
				compute_device = gr.Radio(label="Compute on", choices=['GPU','CPU'], value='GPU', type="index")
				model_type = gr.Dropdown(label="Model", choices=['res101', 'dpt_beit_large_512 (midas 3.1)', 'dpt_beit_large_384 (midas 3.1)', 'dpt_large_384 (midas 3.0)','dpt_hybrid_384 (midas 3.0)','midas_v21','midas_v21_small'], value='res101', type="index", elem_id="tabmodel_type")
			with gr.Group():
				with gr.Row():
					net_width = gr.Slider(minimum=64, maximum=2048, step=64, label='Net width', value=512)
					net_height = gr.Slider(minimum=64, maximum=2048, step=64, label='Net height', value=512)
				match_size = gr.Checkbox(label="Match input size (size is ignored when using boost)",value=False)
			with gr.Group():
				boost = gr.Checkbox(label="BOOST (multi-resolution merging)",value=True)
			with gr.Group():
				invert_depth = gr.Checkbox(label="Invert DepthMap (black=near, white=far)",value=False)
				with gr.Row():
					combine_output = gr.Checkbox(label="Combine into one image.",value=True)
					combine_output_axis = gr.Radio(label="Combine axis", choices=['Vertical','Horizontal'], value='Horizontal', type="index")
				with gr.Row():
					save_depth = gr.Checkbox(label="Save DepthMap",value=True)
					show_depth = gr.Checkbox(label="Show DepthMap",value=True)
					show_heat = gr.Checkbox(label="Show HeatMap",value=False)
			with gr.Group():
				with gr.Row():
					gen_stereo = gr.Checkbox(label="Generate Stereo side-by-side image",value=False)
					gen_anaglyph = gr.Checkbox(label="Generate Stereo anaglyph image (red/cyan)",value=False)
				with gr.Row():
					stereo_divergence = gr.Slider(minimum=0.05, maximum=10.005, step=0.01, label='Divergence (3D effect)', value=2.5)
				with gr.Row():
					stereo_fill = gr.Dropdown(label="Gap fill technique", choices=['none', 'naive', 'naive_interpolating', 'polylines_soft', 'polylines_sharp'], value='polylines_sharp', type="index", elem_id="stereo_fill_type")
					stereo_balance = gr.Slider(minimum=-1.0, maximum=1.0, step=0.05, label='Balance between eyes', value=0.0)


			with gr.Box():
				gr.HTML("Instructions, comment and share @ <a href='https://github.com/thygate/stable-diffusion-webui-depthmap-script'>https://github.com/thygate/stable-diffusion-webui-depthmap-script</a>")

		return [compute_device, model_type, net_width, net_height, match_size, invert_depth, boost, save_depth, show_depth, show_heat, combine_output, combine_output_axis, gen_stereo, gen_anaglyph, stereo_divergence, stereo_fill, stereo_balance]

	# run from script in txt2img or img2img
	def run(self, p, compute_device, model_type, net_width, net_height, match_size, invert_depth, boost, save_depth, show_depth, show_heat, combine_output, combine_output_axis, gen_stereo, gen_anaglyph, stereo_divergence, stereo_fill, stereo_balance):

		# sd process 
		processed = processing.process_images(p)

		processed.sampler = p.sampler # for create_infotext

		inputimages = []
		for count in range(0, len(processed.images)):
			# skip first grid image
			if count == 0 and len(processed.images) > 1:
				continue
			inputimages.append(processed.images[count])

		newmaps = run_depthmap(processed, p.outpath_samples, inputimages, None, compute_device, model_type, net_width, net_height, match_size, invert_depth, boost, save_depth, show_depth, show_heat, combine_output, combine_output_axis, gen_stereo, gen_anaglyph, stereo_divergence, stereo_fill, stereo_balance)
		for img in newmaps:
			processed.images.append(img)

		return processed

def run_depthmap(processed, outpath, inputimages, inputnames, compute_device, model_type, net_width, net_height, match_size, invert_depth, boost, save_depth, show_depth, show_heat, combine_output, combine_output_axis, gen_stereo, gen_anaglyph, stereo_divergence, stereo_fill, stereo_balance):

	# unload sd model
	shared.sd_model.cond_stage_model.to(devices.cpu)
	shared.sd_model.first_stage_model.to(devices.cpu)

	print('\n%s' % scriptname)
	
	# init torch device
	global device
	if compute_device == 0:
		device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
	else:
		device = torch.device("cpu")
	print("device: %s" % device)

	# model path and name
	model_dir = "./models/midas"
	if model_type == 0:
		model_dir = "./models/leres"
	# create paths to model if not present
	os.makedirs(model_dir, exist_ok=True)
	os.makedirs('./models/pix2pix', exist_ok=True)

	outimages = []
	try:
		print("Loading model weights from ", end=" ")

        #"res101"
		if model_type == 0: 
			model_path = f"{model_dir}/res101.pth"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://cloudstor.aarnet.edu.au/plus/s/lTIJF4vrvHCAI31/download")
			if compute_device == 0:
				checkpoint = torch.load(model_path)
			else:
				checkpoint = torch.load(model_path,map_location=torch.device('cpu'))
			model = RelDepthModel(backbone='resnext101')
			model.load_state_dict(strip_prefix_if_present(checkpoint['depth_model'], "module."), strict=True)
			del checkpoint
			devices.torch_gc()

        #"dpt_beit_large_512" midas 3.1
		if model_type == 1: 
			model_path = f"{model_dir}/dpt_beit_large_512.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/isl-org/MiDaS/releases/download/v3_1/dpt_beit_large_512.pt")
			model = DPTDepthModel(
				path=model_path,
				backbone="beitl16_512",
				non_negative=True,
			)
			net_w, net_h = 512, 512
			resize_mode = "minimal"
			normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        #"dpt_beit_large_384" midas 3.1
		if model_type == 2: 
			model_path = f"{model_dir}/dpt_beit_large_384.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/isl-org/MiDaS/releases/download/v3_1/dpt_beit_large_384.pt")
			model = DPTDepthModel(
				path=model_path,
				backbone="beitl16_384",
				non_negative=True,
			)
			net_w, net_h = 384, 384
			resize_mode = "minimal"
			normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

		"""
        #"dpt_swin2_large_384" midas 3.1 / doesn't play nice with input size
		if model_type == 2: 
			model_path = f"{model_dir}/dpt_swin2_large_384.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/isl-org/MiDaS/releases/download/v3_1/dpt_swin2_large_384.pt")
			model = DPTDepthModel(
				path=model_path,
				backbone="swin2l24_384",
				non_negative=True,
			)
			net_w, net_h = 384, 384
			resize_mode = "minimal"
			normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
        """

		#"dpt_large_384" midas 3.0
		if model_type == 3: 
			model_path = f"{model_dir}/dpt_large-midas-2f21e586.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/intel-isl/DPT/releases/download/1_0/dpt_large-midas-2f21e586.pt")
			model = DPTDepthModel(
				path=model_path,
				backbone="vitl16_384",
				non_negative=True,
			)
			net_w, net_h = 384, 384
			resize_mode = "minimal"
			normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

		#"dpt_hybrid_384" midas 3.0
		elif model_type == 4: 
			model_path = f"{model_dir}/dpt_hybrid-midas-501f0c75.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/intel-isl/DPT/releases/download/1_0/dpt_hybrid-midas-501f0c75.pt")
			model = DPTDepthModel(
				path=model_path,
				backbone="vitb_rn50_384",
				non_negative=True,
			)
			net_w, net_h = 384, 384
			resize_mode="minimal"
			normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

		#"midas_v21"
		elif model_type == 5: 
			model_path = f"{model_dir}/midas_v21-f6b98070.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21-f6b98070.pt")
			model = MidasNet(model_path, non_negative=True)
			net_w, net_h = 384, 384
			resize_mode="upper_bound"
			normalization = NormalizeImage(
				mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
			)

		#"midas_v21_small"
		elif model_type == 6: 
			model_path = f"{model_dir}/midas_v21_small-70d6b9c8.pt"
			print(model_path)
			if not os.path.exists(model_path):
				download_file(model_path,"https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21_small-70d6b9c8.pt")
			model = MidasNet_small(model_path, features=64, backbone="efficientnet_lite3", exportable=True, non_negative=True, blocks={'expand': True})
			net_w, net_h = 256, 256
			resize_mode="upper_bound"
			normalization = NormalizeImage(
				mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
			)


		# load merge network if boost enabled
		if boost:
			pix2pixmodel_path = './models/pix2pix/latest_net_G.pth'
			if not os.path.exists(pix2pixmodel_path):
				download_file(pix2pixmodel_path,"https://sfu.ca/~yagiz/CVPR21/latest_net_G.pth")
			opt = MyTestOptions().parse()
			if compute_device == 1:
				opt.gpu_ids = [] # cpu mode
			pix2pixmodel = Pix2Pix4DepthModel(opt)
			pix2pixmodel.save_dir = './models/pix2pix'
			pix2pixmodel.load_networks('latest')
			pix2pixmodel.eval()

		devices.torch_gc()

		# prepare for evaluation
		model.eval()
	
		# optimize
		if device == torch.device("cuda"):
			model = model.to(memory_format=torch.channels_last)  
			if not cmd_opts.no_half and model_type != 0 and not boost:
				model = model.half()

		model.to(device)

		print("Computing depthmap(s) ..")
		# iterate over input (generated) images
		numimages = len(inputimages)
		for count in trange(0, numimages):

			#if numimages > 1:
			#	print("\nDepthmap", count+1, '/', numimages)
			print('\n')

			# override net size
			if (match_size):
				net_width, net_height = inputimages[count].width, inputimages[count].height

			# input image
			img = cv2.cvtColor(np.asarray(inputimages[count]), cv2.COLOR_BGR2RGB) / 255.0
			
			# compute
			if not boost:
				if model_type == 0:
					prediction = estimateleres(img, model, net_width, net_height)
				else:
					prediction = estimatemidas(img, model, net_width, net_height, resize_mode, normalization)
			else:
				prediction = estimateboost(img, model, model_type, pix2pixmodel)

			# output
			depth = prediction
			numbytes=2
			depth_min = depth.min()
			depth_max = depth.max()
			max_val = (2**(8*numbytes))-1

			# check output before normalizing and mapping to 16 bit
			if depth_max - depth_min > np.finfo("float").eps:
				out = max_val * (depth - depth_min) / (depth_max - depth_min)
			else:
				out = np.zeros(depth.shape)
			
			# single channel, 16 bit image
			img_output = out.astype("uint16")

			# invert depth map
			if invert_depth ^ model_type == 0:
				img_output = cv2.bitwise_not(img_output)

			"""
            # sparse bilateral filtering
            print("Bilateral filter (TEST) ..")
			sparse_iter = 5
			filter_size = [7, 7, 5, 5, 5]
			depth_threshold = 0.04
			sigma_s = 4.0
			sigma_r = 0.5
			with np.errstate(divide='ignore', invalid='ignore'):
				vis_photos, vis_depths = sparse_bilateral_filtering(img_output.astype(np.float32).copy(), img.copy(), filter_size, depth_threshold, sigma_s, sigma_r, num_iter=sparse_iter, spdb=False)
			fimg = Image.fromarray(vis_depths[-1].astype("uint16"))
			outimages.append(fimg)
			img_output = vis_depths[-1].astype("uint16")
            """

			# three channel, 8 bits per channel image
			img_output2 = np.zeros_like(inputimages[count])
			img_output2[:,:,0] = img_output / 256.0
			img_output2[:,:,1] = img_output / 256.0
			img_output2[:,:,2] = img_output / 256.0

			# get generation parameters
			if processed is not None and hasattr(processed, 'all_prompts') and opts.enable_pnginfo:
				info = create_infotext(processed, processed.all_prompts, processed.all_seeds, processed.all_subseeds, "", 0, count)
			else:
				info = None

			basename = 'depthmap'
			if inputnames is not None:
				if inputnames[count] is not None:
					p = Path(inputnames[count])
					basename = p.stem

			if not combine_output:
				if show_depth:
					outimages.append(Image.fromarray(img_output))
				if save_depth and processed is not None:
					# only save 16 bit single channel image when PNG format is selected
					if opts.samples_format == "png":
						images.save_image(Image.fromarray(img_output), outpath, "", processed.all_seeds[count], processed.all_prompts[count], opts.samples_format, info=info, p=processed, suffix="_depth")
					else:
						images.save_image(Image.fromarray(img_output2), outpath, "", processed.all_seeds[count], processed.all_prompts[count], opts.samples_format, info=info, p=processed, suffix="_depth")
				elif save_depth:
					# from depth tab
					# only save 16 bit single channel image when PNG format is selected
					if opts.samples_format == "png":
						images.save_image(Image.fromarray(img_output), path=outpath, basename=basename, seed=None, prompt=None, extension=opts.samples_format, info=info, short_filename=True,no_prompt=True, grid=False, pnginfo_section_name="extras", existing_info=None, forced_filename=None)
					else:
						images.save_image(Image.fromarray(img_output2), path=outpath, basename=basename, seed=None, prompt=None, extension=opts.samples_format, info=info, short_filename=True,no_prompt=True, grid=False, pnginfo_section_name="extras", existing_info=None, forced_filename=None)
			else:
				img_concat = np.concatenate((inputimages[count], img_output2), axis=combine_output_axis)
				if show_depth:
					outimages.append(Image.fromarray(img_concat))
				if save_depth and processed is not None:
					images.save_image(Image.fromarray(img_concat), outpath, "", processed.all_seeds[count], processed.all_prompts[count], opts.samples_format, info=info, p=processed, suffix="_depth")
				elif save_depth:
					# from tab
					images.save_image(Image.fromarray(img_concat), path=outpath, basename=basename, seed=None, prompt=None, extension=opts.samples_format, info=info, short_filename=True,no_prompt=True, grid=False, pnginfo_section_name="extras", existing_info=None, forced_filename=None)
			if show_heat:
				colormap = plt.get_cmap('inferno')
				heatmap = (colormap(img_output2[:,:,0] / 256.0) * 2**16).astype(np.uint16)[:,:,:3]
				outimages.append(heatmap)

			if gen_stereo or gen_anaglyph:
				print("Generating Stereo image..")
				#img_output = cv2.blur(img_output, (3, 3))
				balance = (stereo_balance + 1) / 2
				original_image = np.asarray(inputimages[count])
				left_image = original_image if balance < 0.001 else \
					apply_stereo_divergence(original_image, img_output, - stereo_divergence * balance, stereo_fill)
				right_image = original_image if balance > 0.999 else \
					apply_stereo_divergence(original_image, img_output, stereo_divergence * (1 - balance), stereo_fill)
				stereo_img = np.hstack([left_image, right_image])

				if gen_stereo:
					outimages.append(stereo_img)
				if gen_anaglyph:
					print("Generating Anaglyph image..")
					anaglyph_img = overlap(left_image, right_image)
					outimages.append(anaglyph_img)
				if (processed is not None):
					if gen_stereo:
						images.save_image(Image.fromarray(stereo_img), outpath, "", processed.all_seeds[count], processed.all_prompts[count], opts.samples_format, info=info, p=processed, suffix="_stereo")
					if gen_anaglyph:
						images.save_image(Image.fromarray(anaglyph_img), outpath, "", processed.all_seeds[count], processed.all_prompts[count], opts.samples_format, info=info, p=processed, suffix="_anaglyph")
				else:
					# from tab
					if gen_stereo:
						images.save_image(Image.fromarray(stereo_img), path=outpath, basename=basename, seed=None, prompt=None, extension=opts.samples_format, info=info, short_filename=True,no_prompt=True, grid=False, pnginfo_section_name="extras", existing_info=None, forced_filename=None, suffix="_stereo")
					if gen_anaglyph:
						images.save_image(Image.fromarray(anaglyph_img), path=outpath, basename=basename, seed=None, prompt=None, extension=opts.samples_format, info=info, short_filename=True,no_prompt=True, grid=False, pnginfo_section_name="extras", existing_info=None, forced_filename=None, suffix="_anaglyph")

		print("Done.")

	except RuntimeError as e:
		if 'out of memory' in str(e):
			print("ERROR: out of memory, could not generate depthmap !")
		else:
			print(e)

	finally:
		if 'model' in locals():
			del model
		if boost and 'pix2pixmodel' in locals():
			del pix2pixmodel

		gc.collect()
		devices.torch_gc()

		# reload sd model
		shared.sd_model.cond_stage_model.to(devices.device)
		shared.sd_model.first_stage_model.to(devices.device)

	return outimages

def apply_stereo_divergence(original_image, depth, divergence, fill_technique):
    depth_min = depth.min()
    depth_max = depth.max()
    depth = (depth - depth_min) / (depth_max - depth_min)
    divergence_px = (divergence / 100.0) * original_image.shape[1]

    if fill_technique in [0, 1, 2]:
        return apply_stereo_divergence_naive(original_image, depth, divergence_px, fill_technique)
    if fill_technique in [3, 4]:
        return apply_stereo_divergence_polylines(original_image, depth, divergence_px, fill_technique)

@njit
def apply_stereo_divergence_naive(original_image, normalized_depth, divergence_px: float, fill_technique):
    h, w, c = original_image.shape

    derived_image = np.zeros_like(original_image)
    filled = np.zeros(h * w, dtype=np.uint8)

    for row in prange(h):
        # Swipe order should ensure that pixels that are closer overwrite
        # (at their destination) pixels that are less close
        for col in range(w) if divergence_px < 0 else range(w - 1, -1, -1):
            col_d = col + int((1 - normalized_depth[row][col] ** 2) * divergence_px)
            if 0 <= col_d < w:
                derived_image[row][col_d] = original_image[row][col]
                filled[row * w + col_d] = 1

    # Fill the gaps
    if fill_technique == 2:  # naive_interpolating
        for row in range(h):
            for l_pointer in range(w):
                # This if (and the next if) performs two checks that are almost the same - for performance reasons
                if sum(derived_image[row][l_pointer]) != 0 or filled[row * w + l_pointer]:
                    continue
                l_border = derived_image[row][l_pointer - 1] if l_pointer > 0 else np.zeros(3, dtype=np.uint8)
                r_border = np.zeros(3, dtype=np.uint8)
                r_pointer = l_pointer + 1
                while r_pointer < w:
                    if sum(derived_image[row][r_pointer]) != 0 and filled[row * w + r_pointer]:
                        r_border = derived_image[row][r_pointer]
                        break
                    r_pointer += 1
                if sum(l_border) == 0:
                    l_border = r_border
                elif sum(r_border) == 0:
                    r_border = l_border
                # Example illustrating positions of pointers at this point in code:
                # is filled?  : +   -   -   -   -   +
                # pointers    :     l               r
                # interpolated: 0   1   2   3   4   5
                # In total: 5 steps between two filled pixels
                total_steps = 1 + r_pointer - l_pointer
                step = (r_border.astype(np.float_) - l_border) / total_steps
                for col in range(l_pointer, r_pointer):
                    derived_image[row][col] = l_border + (step * (col - l_pointer + 1)).astype(np.uint8)
        return derived_image
    elif fill_technique == 1:  # naive
        derived_fix = np.copy(derived_image)
        for pos in np.where(filled == 0)[0]:
            row = pos // w
            col = pos % w
            row_times_w = row * w
            for offset in range(1, abs(int(divergence_px)) + 2):
                r_offset = col + offset
                l_offset = col - offset
                if r_offset < w and filled[row_times_w + r_offset]:
                    derived_fix[row][col] = derived_image[row][r_offset]
                    break
                if 0 <= l_offset and filled[row_times_w + l_offset]:
                    derived_fix[row][col] = derived_image[row][l_offset]
                    break
        return derived_fix
    else:  # none
        return derived_image

@njit(parallel=True)  # fastmath=True does not reasonably improve performance
def apply_stereo_divergence_polylines(original_image, normalized_depth, divergence_px: float, fill_technique):
    # This code treats rows of the image as polylines
    # It generates polylines, morphs them (applies divergence) to them, and then rasterizes them
    EPSILON = 1e-7
    PIXEL_HALF_WIDTH = 0.45 if fill_technique == 4 else 0.0
    # PERF_COUNTERS = [0, 0, 0]

    h, w, c = original_image.shape
    derived_image = np.zeros_like(original_image)
    for row in prange(h):
        # generating the vertices of the morphed polyline
        # format: new coordinate of the vertex, divergence (closeness), column of pixel that contains the point's color
        pt = np.zeros((5 + 2 * w, 3), dtype=np.float_)
        pt_end: int = 0
        pt[pt_end] = [-3.0 * abs(divergence_px), 0.0, 0.0]
        pt_end += 1
        for col in range(0, w):
            coord_d = (1 - normalized_depth[row][col] ** 2) * divergence_px
            coord_x = col + 0.5 + coord_d
            if PIXEL_HALF_WIDTH < EPSILON:
                pt[pt_end] = [coord_x, abs(coord_d), col]
                pt_end += 1
            else:
                pt[pt_end] = [coord_x - PIXEL_HALF_WIDTH, abs(coord_d), col]
                pt[pt_end + 1] = [coord_x + PIXEL_HALF_WIDTH, abs(coord_d), col]
                pt_end += 2
        pt[pt_end] = [w + 3.0 * abs(divergence_px), 0.0, w - 1]
        pt_end += 1

        # generating the segments of the morphed polyline
        # format: coord_x, coord_d, color_i of the first point, then the same for the second point
        sg_end: int = pt_end - 1
        sg = np.zeros((sg_end, 6), dtype=np.float_)
        for i in range(sg_end):
            sg[i] += np.concatenate((pt[i], pt[i + 1]))
        # Here is an informal proof that this (morphed) polyline does not self-intersect:
        # Draw a plot with two axes: coord_x and coord_d. Now draw the original line - it will be positioned at the
        # bottom of the graph (that is, for every point coord_d == 0). Now draw the morphed line using the vertices of
        # the original polyline. Observe that for each vertex in the new polyline, its increments
        # (from the corresponding vertex in the old polyline) over coord_x and coord_d are in direct proportion.
        # In fact, this proportion is equal for all the vertices and it is equal either -1 or +1,
        # depending on the sign of divergence_px. Now draw the lines from each old vertex to a corresponding new vertex.
        # Since the proportions are equal, these lines have the same angle with an axe and are parallel.
        # So, these lines do not intersect. Now rotate the plot by 45 or -45 degrees and observe that
        # each dot of the polyline is further right from the last dot,
        # which makes it impossible for the polyline to self-interset. QED.

        # sort segments and points using insertion sort
        # has a very good performance in practice, since these are almost sorted to begin with
        for i in range(1, sg_end):
            u = i - 1
            while pt[u][0] > pt[u + 1][0] and 0 <= u:
                pt[u], pt[u + 1] = np.copy(pt[u + 1]), np.copy(pt[u])
                sg[u], sg[u + 1] = np.copy(sg[u + 1]), np.copy(sg[u])
                u -= 1

        # rasterizing
        # at each point in time we keep track of segments that are "active" (or "current")
        csg = np.zeros((5 * int(abs(divergence_px)) + 25, 6), dtype=np.float_)
        csg_end: int = 0
        sg_pointer: int = 0
        # and index of the point that should be processed next
        pt_i: int = 0
        for col in range(w):  # iterate over regions (that will be rasterizeed into pixels)
            color = np.full(c, 0.5, dtype=np.float_)  # we start with 0.5 because of how floats are converted to ints
            while pt[pt_i][0] < col:
                pt_i += 1
            pt_i -= 1  # pt_i now points to the dot before the region start
            # Finding segment' parts that contribute color to the region
            while pt[pt_i][0] < col + 1:
                coord_from = max(col, pt[pt_i][0]) + EPSILON
                coord_to = min(col + 1, pt[pt_i + 1][0]) - EPSILON
                significance = coord_to - coord_from
                # the color at center point is the same as the average of color of segment part
                coord_center = coord_from + 0.5 * significance

                # adding semgents that now may contribute
                while sg_pointer < sg_end and sg[sg_pointer][0] < coord_center:
                    csg[csg_end] = sg[sg_pointer]
                    sg_pointer += 1
                    csg_end += 1
                # removing segments that will no longer contribute
                csg_i = 0
                while csg_i < csg_end:
                    if csg[csg_i][3] < coord_center:
                        csg[csg_i] = csg[csg_end - 1]
                        csg_end -= 1
                    else:
                        csg_i += 1
                # finding the closest segment (segment with most divergence)
                # note that this segment will be the closest from coord_from right up to coord_to, since there
                # no new segments "appearing" inbetween these two and _the polyline does not self-intersect_
                best_csg_i: int = 0
                # PERF_COUNTERS[0] += 1
                if csg_end != 1:
                    # PERF_COUNTERS[1] += 1
                    best_csg_closeness: float = -EPSILON
                    for csg_i in range(csg_end):
                        ip_k = (coord_center - csg[csg_i][0]) / (csg[csg_i][3] - csg[csg_i][0])
                        # assert 0.0 <= ip_k <= 1.0
                        closeness = (1.0 - ip_k) * csg[csg_i][1] + ip_k * csg[csg_i][4]
                        if best_csg_closeness < closeness and 0.0 < ip_k < 1.0:
                            best_csg_closeness = closeness
                            best_csg_i = csg_i
                # getting the color
                col_l: int = int(csg[best_csg_i][2] + EPSILON)
                col_r: int = int(csg[best_csg_i][5] + EPSILON)
                if col_l == col_r:
                    color += original_image[row][col_l] * significance
                else:
                    # PERF_COUNTERS[2] += 1
                    ip_k = (coord_center - csg[best_csg_i][0]) / (csg[best_csg_i][3] - csg[best_csg_i][0])
                    color += (original_image[row][col_l] * (1.0 - ip_k) + original_image[row][col_r] * ip_k) \
                        * significance
                pt_i += 1
            derived_image[row][col] = np.asarray(color, dtype=np.uint8)
    # print(PERF_COUNTERS)
    return derived_image

@njit(parallel=True)
def overlap(im1, im2):
    width1 = im1.shape[1]
    height1 = im1.shape[0]
    width2 = im2.shape[1]
    height2 = im2.shape[0]

    # final image
    composite = np.zeros((height2, width2, 3), np.uint8)

    # iterate through "left" image, filling in red values of final image
    for i in prange(height1):
        for j in prange(width1):
            #try:
                composite[i, j, 0] = im1[i, j, 0]
            #except IndexError:
            #    pass

    # iterate through "right" image, filling in blue/green values of final image
    for i in prange(height2):
        for j in prange(width2):
            #try:
                composite[i, j, 1] = im2[i, j, 1]
                composite[i, j, 2] = im2[i, j, 2]
            #except IndexError:
            #    pass

    return composite

def run_generate(depthmap_mode, 
				depthmap_image,
                image_batch,
                depthmap_batch_input_dir,
                depthmap_batch_output_dir,
				compute_device, 
				model_type,
				net_width, 
				net_height, 
				match_size,
				invert_depth,
				boost, 
				save_depth, 
				show_depth, 
				show_heat, 
				combine_output, 
				combine_output_axis,
				gen_stereo, 
				gen_anaglyph,
				stereo_divergence,
				stereo_fill,
				stereo_balance
				):

	imageArr = []
	# Also keep track of original file names
	imageNameArr = []
	outputs = []

	if depthmap_mode == 1:
		#convert file to pillow image
		for img in image_batch:
			image = Image.open(img)
			imageArr.append(image)
			imageNameArr.append(os.path.splitext(img.orig_name)[0])
	elif depthmap_mode == 2:
		assert not shared.cmd_opts.hide_ui_dir_config, '--hide-ui-dir-config option must be disabled'

		if depthmap_batch_input_dir == '':
			return outputs, "Please select an input directory.", ''
		image_list = shared.listfiles(depthmap_batch_input_dir)
		for img in image_list:
			try:
				image = Image.open(img)
			except Exception:
				continue
			imageArr.append(image)
			imageNameArr.append(img)
	else:
		imageArr.append(depthmap_image)
		imageNameArr.append(None)

	if depthmap_mode == 2 and depthmap_batch_output_dir != '':
		outpath = depthmap_batch_output_dir
	else:
		outpath = opts.outdir_samples or opts.outdir_extras_samples


	outputs = run_depthmap(None, outpath, imageArr, imageNameArr, compute_device, model_type, net_width, net_height, match_size, invert_depth, boost, save_depth, show_depth, show_heat, combine_output, combine_output_axis, gen_stereo, gen_anaglyph, stereo_divergence, stereo_fill, stereo_balance)

	return outputs, plaintext_to_html('info'), ''

def on_ui_settings():
    section = ('depthmap-script', "Depthmap extension")
    shared.opts.add_option("depthmap_script_boost_rmax", shared.OptionInfo(1600, "Maximum wholesize for boost.", section=section))

def on_ui_tabs():
    with gr.Blocks(analytics_enabled=False) as depthmap_interface:
        dummy_component = gr.Label(visible=False)
        with gr.Row().style(equal_height=False):
            with gr.Column(variant='panel'):
                with gr.Tabs(elem_id="mode_depthmap"):
                    with gr.TabItem('Single Image'):
                        depthmap_image = gr.Image(label="Source", source="upload", interactive=True, type="pil")

                    with gr.TabItem('Batch Process'):
                        image_batch = gr.File(label="Batch Process", file_count="multiple", interactive=True, type="file")

                    with gr.TabItem('Batch from Directory'):
                        depthmap_batch_input_dir = gr.Textbox(label="Input directory", **shared.hide_dirs, placeholder="A directory on the same machine where the server is running.")
                        depthmap_batch_output_dir = gr.Textbox(label="Output directory", **shared.hide_dirs, placeholder="Leave blank to save images to the default path.")

                submit = gr.Button('Generate', elem_id="depthmap_generate", variant='primary')

                with gr.Row():
                    compute_device = gr.Radio(label="Compute on", choices=['GPU','CPU'], value='GPU', type="index")
                    model_type = gr.Dropdown(label="Model", choices=['res101', 'dpt_beit_large_512 (midas 3.1)', 'dpt_beit_large_384 (midas 3.1)', 'dpt_large_384 (midas 3.0)','dpt_hybrid_384 (midas 3.0)','midas_v21','midas_v21_small'], value='res101', type="index", elem_id="tabmodel_type")
                with gr.Group():
                    with gr.Row():
                        net_width = gr.Slider(minimum=64, maximum=2048, step=64, label='Net width', value=512)
                        net_height = gr.Slider(minimum=64, maximum=2048, step=64, label='Net height', value=512)
                    match_size = gr.Checkbox(label="Match input size (size is ignored when using boost)",value=False)
                with gr.Group():
                    boost = gr.Checkbox(label="BOOST (multi-resolution merging)",value=True)
                with gr.Group():
                    invert_depth = gr.Checkbox(label="Invert DepthMap (black=near, white=far)",value=False)
                    with gr.Row():
                        combine_output = gr.Checkbox(label="Combine into one image.",value=True)
                        combine_output_axis = gr.Radio(label="Combine axis", choices=['Vertical','Horizontal'], value='Horizontal', type="index")
                    with gr.Row():
                        save_depth = gr.Checkbox(label="Save DepthMap",value=True)
                        show_depth = gr.Checkbox(label="Show DepthMap",value=True)
                        show_heat = gr.Checkbox(label="Show HeatMap",value=False)
                with gr.Group():
                    with gr.Row():
                        gen_stereo = gr.Checkbox(label="Generate Stereo side-by-side image",value=False)
                        gen_anaglyph = gr.Checkbox(label="Generate Stereo anaglyph image (red/cyan)",value=False)
                    with gr.Row():
                        stereo_divergence = gr.Slider(minimum=0.05, maximum=10.005, step=0.01, label='Divergence (3D effect)', value=2.5)
                    with gr.Row():
                        stereo_fill = gr.Dropdown(label="Gap fill technique", choices=['none', 'naive', 'naive_interpolating', 'polylines_soft', 'polylines_sharp'], value='polylines_sharp', type="index", elem_id="stereo_fill_type")
                        stereo_balance = gr.Slider(minimum=-1.0, maximum=1.0, step=0.05, label='Balance between eyes', value=0.0)

                with gr.Box():
                    gr.HTML("Instructions, comment and share @ <a href='https://github.com/thygate/stable-diffusion-webui-depthmap-script'>https://github.com/thygate/stable-diffusion-webui-depthmap-script</a>")


            #result_images, html_info_x, html_info = modules.ui.create_output_panel("depthmap", opts.outdir_extras_samples)
            with gr.Column(variant='panel'):
                with gr.Group():
                    result_images = gr.Gallery(label='Output', show_label=False, elem_id=f"depthmap_gallery").style(grid=4)
                with gr.Column():
                    html_info_x = gr.HTML()
                    html_info = gr.HTML()
			

        submit.click(
            fn=wrap_gradio_gpu_call(run_generate),
            _js="get_depthmap_tab_index",
            inputs=[
                dummy_component,
                depthmap_image,
                image_batch,
                depthmap_batch_input_dir,
                depthmap_batch_output_dir,
				compute_device, 
				model_type,
				net_width, 
				net_height, 
				match_size,
				invert_depth,
				boost, 
				save_depth, 
				show_depth, 
				show_heat, 
				combine_output, 
				combine_output_axis,
				gen_stereo, 
				gen_anaglyph,
				stereo_divergence,
				stereo_fill,
				stereo_balance
            ],
            outputs=[
                result_images,
                html_info_x,
                html_info,
            ]
        )

    return (depthmap_interface , "Depth", "depthmap_interface"),

script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_ui_tabs(on_ui_tabs)


def download_file(filename, url):
	print("Downloading", url, "to", filename)
	torch.hub.download_url_to_file(url, filename)
	# check if file exists
	if not os.path.exists(filename):
		raise RuntimeError('Download failed. Try again later or manually download the file to that location.')

def scale_torch(img):
	"""
	Scale the image and output it in torch.tensor.
	:param img: input rgb is in shape [H, W, C], input depth/disp is in shape [H, W]
	:param scale: the scale factor. float
	:return: img. [C, H, W]
	"""
	if len(img.shape) == 2:
		img = img[np.newaxis, :, :]
	if img.shape[2] == 3:
		transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.485, 0.456, 0.406) , (0.229, 0.224, 0.225) )])
		img = transform(img.astype(np.float32))
	else:
		img = img.astype(np.float32)
		img = torch.from_numpy(img)
	return img
	
def estimateleres(img, model, w, h):
	# leres transform input
	rgb_c = img[:, :, ::-1].copy()
	A_resize = cv2.resize(rgb_c, (w, h))
	img_torch = scale_torch(A_resize)[None, :, :, :] 
	
	# compute
	with torch.no_grad():
		if device == torch.device("cuda"):
			img_torch = img_torch.cuda()
		prediction = model.depth_model(img_torch)

	prediction = prediction.squeeze().cpu().numpy()
	prediction = cv2.resize(prediction, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC)

	return prediction

def estimatemidas(img, model, w, h, resize_mode, normalization):
	# init transform
	transform = Compose(
		[
			Resize(
				w,
				h,
				resize_target=None,
				keep_aspect_ratio=True,
				ensure_multiple_of=32,
				resize_method=resize_mode,
				image_interpolation_method=cv2.INTER_CUBIC,
			),
			normalization,
			PrepareForNet(),
		]
	)

	# transform input
	img_input = transform({"image": img})["image"]

	# compute
	precision_scope = torch.autocast if shared.cmd_opts.precision == "autocast" and device == torch.device("cuda") else contextlib.nullcontext
	with torch.no_grad(), precision_scope("cuda"):
		sample = torch.from_numpy(img_input).to(device).unsqueeze(0)
		if device == torch.device("cuda"):
			sample = sample.to(memory_format=torch.channels_last) 
			if not cmd_opts.no_half:
				sample = sample.half()
		prediction = model.forward(sample)
		prediction = (
			torch.nn.functional.interpolate(
				prediction.unsqueeze(1),
				size=img.shape[:2],
				mode="bicubic",
				align_corners=False,
			)
			.squeeze()
			.cpu()
			.numpy()
		)

	return prediction

def estimatemidasBoost(img, model, w, h):
	# init transform
    transform = Compose(
        [
            Resize(
                w,
                h,
                resize_target=None,
                keep_aspect_ratio=True,
                ensure_multiple_of=32,
                resize_method="upper_bound",
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            NormalizeImage(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            PrepareForNet(),
        ]
    )

	# transform input
    img_input = transform({"image": img})["image"]

    # compute
    with torch.no_grad():
        sample = torch.from_numpy(img_input).to(device).unsqueeze(0)
        if device == torch.device("cuda"):
            sample = sample.to(memory_format=torch.channels_last) 
        prediction = model.forward(sample)

    prediction = prediction.squeeze().cpu().numpy()    
    prediction = cv2.resize(prediction, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC)

    # normalization
    depth_min = prediction.min()
    depth_max = prediction.max()

    if depth_max - depth_min > np.finfo("float").eps:
        prediction = (prediction - depth_min) / (depth_max - depth_min)
    else:
        prediction = 0

    return prediction

def generatemask(size):
    # Generates a Guassian mask
    mask = np.zeros(size, dtype=np.float32)
    sigma = int(size[0]/16)
    k_size = int(2 * np.ceil(2 * int(size[0]/16)) + 1)
    mask[int(0.15*size[0]):size[0] - int(0.15*size[0]), int(0.15*size[1]): size[1] - int(0.15*size[1])] = 1
    mask = cv2.GaussianBlur(mask, (int(k_size), int(k_size)), sigma)
    mask = (mask - mask.min()) / (mask.max() - mask.min())
    mask = mask.astype(np.float32)
    return mask

def resizewithpool(img, size):
    i_size = img.shape[0]
    n = int(np.floor(i_size/size))

    out = skimage.measure.block_reduce(img, (n, n), np.max)
    return out

def rgb2gray(rgb):
    # Converts rgb to gray
    return np.dot(rgb[..., :3], [0.2989, 0.5870, 0.1140])

def calculateprocessingres(img, basesize, confidence=0.1, scale_threshold=3, whole_size_threshold=3000):
    # Returns the R_x resolution described in section 5 of the main paper.

    # Parameters:
    #    img :input rgb image
    #    basesize : size the dilation kernel which is equal to receptive field of the network.
    #    confidence: value of x in R_x; allowed percentage of pixels that are not getting any contextual cue.
    #    scale_threshold: maximum allowed upscaling on the input image ; it has been set to 3.
    #    whole_size_threshold: maximum allowed resolution. (R_max from section 6 of the main paper)

    # Returns:
    #    outputsize_scale*speed_scale :The computed R_x resolution
    #    patch_scale: K parameter from section 6 of the paper

    # speed scale parameter is to process every image in a smaller size to accelerate the R_x resolution search
    speed_scale = 32
    image_dim = int(min(img.shape[0:2]))

    gray = rgb2gray(img)
    grad = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)) + np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
    grad = cv2.resize(grad, (image_dim, image_dim), cv2.INTER_AREA)

    # thresholding the gradient map to generate the edge-map as a proxy of the contextual cues
    m = grad.min()
    M = grad.max()
    middle = m + (0.4 * (M - m))
    grad[grad < middle] = 0
    grad[grad >= middle] = 1

    # dilation kernel with size of the receptive field
    kernel = np.ones((int(basesize/speed_scale), int(basesize/speed_scale)), np.float)
    # dilation kernel with size of the a quarter of receptive field used to compute k
    # as described in section 6 of main paper
    kernel2 = np.ones((int(basesize / (4*speed_scale)), int(basesize / (4*speed_scale))), np.float)

    # Output resolution limit set by the whole_size_threshold and scale_threshold.
    threshold = min(whole_size_threshold, scale_threshold * max(img.shape[:2]))

    outputsize_scale = basesize / speed_scale
    for p_size in range(int(basesize/speed_scale), int(threshold/speed_scale), int(basesize / (2*speed_scale))):
        grad_resized = resizewithpool(grad, p_size)
        grad_resized = cv2.resize(grad_resized, (p_size, p_size), cv2.INTER_NEAREST)
        grad_resized[grad_resized >= 0.5] = 1
        grad_resized[grad_resized < 0.5] = 0

        dilated = cv2.dilate(grad_resized, kernel, iterations=1)
        meanvalue = (1-dilated).mean()
        if meanvalue > confidence:
            break
        else:
            outputsize_scale = p_size

    grad_region = cv2.dilate(grad_resized, kernel2, iterations=1)
    patch_scale = grad_region.mean()

    return int(outputsize_scale*speed_scale), patch_scale

# Generate a double-input depth estimation
def doubleestimate(img, size1, size2, pix2pixsize, model, net_type, pix2pixmodel):
    # Generate the low resolution estimation
    estimate1 = singleestimate(img, size1, model, net_type)
    # Resize to the inference size of merge network.
    estimate1 = cv2.resize(estimate1, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)

    # Generate the high resolution estimation
    estimate2 = singleestimate(img, size2, model, net_type)
    # Resize to the inference size of merge network.
    estimate2 = cv2.resize(estimate2, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)

    # Inference on the merge model
    pix2pixmodel.set_input(estimate1, estimate2)
    pix2pixmodel.test()
    visuals = pix2pixmodel.get_current_visuals()
    prediction_mapped = visuals['fake_B']
    prediction_mapped = (prediction_mapped+1)/2
    prediction_mapped = (prediction_mapped - torch.min(prediction_mapped)) / (
                torch.max(prediction_mapped) - torch.min(prediction_mapped))
    prediction_mapped = prediction_mapped.squeeze().cpu().numpy()

    return prediction_mapped

# Generate a single-input depth estimation
def singleestimate(img, msize, model, net_type):
	if net_type == 0:
		return estimateleres(img, model, msize, msize)
	else:
		return estimatemidasBoost(img, model, msize, msize)

def applyGridpatch(blsize, stride, img, box):
    # Extract a simple grid patch.
    counter1 = 0
    patch_bound_list = {}
    for k in range(blsize, img.shape[1] - blsize, stride):
        for j in range(blsize, img.shape[0] - blsize, stride):
            patch_bound_list[str(counter1)] = {}
            patchbounds = [j - blsize, k - blsize, j - blsize + 2 * blsize, k - blsize + 2 * blsize]
            patch_bound = [box[0] + patchbounds[1], box[1] + patchbounds[0], patchbounds[3] - patchbounds[1],
                           patchbounds[2] - patchbounds[0]]
            patch_bound_list[str(counter1)]['rect'] = patch_bound
            patch_bound_list[str(counter1)]['size'] = patch_bound[2]
            counter1 = counter1 + 1
    return patch_bound_list

# Generating local patches to perform the local refinement described in section 6 of the main paper.
def generatepatchs(img, base_size):
    
    # Compute the gradients as a proxy of the contextual cues.
    img_gray = rgb2gray(img)
    whole_grad = np.abs(cv2.Sobel(img_gray, cv2.CV_64F, 0, 1, ksize=3)) +\
        np.abs(cv2.Sobel(img_gray, cv2.CV_64F, 1, 0, ksize=3))

    threshold = whole_grad[whole_grad > 0].mean()
    whole_grad[whole_grad < threshold] = 0

    # We use the integral image to speed-up the evaluation of the amount of gradients for each patch.
    gf = whole_grad.sum()/len(whole_grad.reshape(-1))
    grad_integral_image = cv2.integral(whole_grad)

    # Variables are selected such that the initial patch size would be the receptive field size
    # and the stride is set to 1/3 of the receptive field size.
    blsize = int(round(base_size/2))
    stride = int(round(blsize*0.75))

    # Get initial Grid
    patch_bound_list = applyGridpatch(blsize, stride, img, [0, 0, 0, 0])

    # Refine initial Grid of patches by discarding the flat (in terms of gradients of the rgb image) ones. Refine
    # each patch size to ensure that there will be enough depth cues for the network to generate a consistent depth map.
    print("Selecting patches ...")
    patch_bound_list = adaptiveselection(grad_integral_image, patch_bound_list, gf)

    # Sort the patch list to make sure the merging operation will be done with the correct order: starting from biggest
    # patch
    patchset = sorted(patch_bound_list.items(), key=lambda x: getitem(x[1], 'size'), reverse=True)
    return patchset

def getGF_fromintegral(integralimage, rect):
    # Computes the gradient density of a given patch from the gradient integral image.
    x1 = rect[1]
    x2 = rect[1]+rect[3]
    y1 = rect[0]
    y2 = rect[0]+rect[2]
    value = integralimage[x2, y2]-integralimage[x1, y2]-integralimage[x2, y1]+integralimage[x1, y1]
    return value

# Adaptively select patches
def adaptiveselection(integral_grad, patch_bound_list, gf):
    patchlist = {}
    count = 0
    height, width = integral_grad.shape

    search_step = int(32/factor)

    # Go through all patches
    for c in range(len(patch_bound_list)):
        # Get patch
        bbox = patch_bound_list[str(c)]['rect']

        # Compute the amount of gradients present in the patch from the integral image.
        cgf = getGF_fromintegral(integral_grad, bbox)/(bbox[2]*bbox[3])

        # Check if patching is beneficial by comparing the gradient density of the patch to
        # the gradient density of the whole image
        if cgf >= gf:
            bbox_test = bbox.copy()
            patchlist[str(count)] = {}

            # Enlarge each patch until the gradient density of the patch is equal
            # to the whole image gradient density
            while True:

                bbox_test[0] = bbox_test[0] - int(search_step/2)
                bbox_test[1] = bbox_test[1] - int(search_step/2)

                bbox_test[2] = bbox_test[2] + search_step
                bbox_test[3] = bbox_test[3] + search_step

                # Check if we are still within the image
                if bbox_test[0] < 0 or bbox_test[1] < 0 or bbox_test[1] + bbox_test[3] >= height \
                        or bbox_test[0] + bbox_test[2] >= width:
                    break

                # Compare gradient density
                cgf = getGF_fromintegral(integral_grad, bbox_test)/(bbox_test[2]*bbox_test[3])
                if cgf < gf:
                    break
                bbox = bbox_test.copy()

            # Add patch to selected patches
            patchlist[str(count)]['rect'] = bbox
            patchlist[str(count)]['size'] = bbox[2]
            count = count + 1
    
    # Return selected patches
    return patchlist

def impatch(image, rect):
    # Extract the given patch pixels from a given image.
    w1 = rect[0]
    h1 = rect[1]
    w2 = w1 + rect[2]
    h2 = h1 + rect[3]
    image_patch = image[h1:h2, w1:w2]
    return image_patch

class ImageandPatchs:
    def __init__(self, root_dir, name, patchsinfo, rgb_image, scale=1):
        self.root_dir = root_dir
        self.patchsinfo = patchsinfo
        self.name = name
        self.patchs = patchsinfo
        self.scale = scale

        self.rgb_image = cv2.resize(rgb_image, (round(rgb_image.shape[1]*scale), round(rgb_image.shape[0]*scale)),
                                    interpolation=cv2.INTER_CUBIC)

        self.do_have_estimate = False
        self.estimation_updated_image = None
        self.estimation_base_image = None

    def __len__(self):
        return len(self.patchs)

    def set_base_estimate(self, est):
        self.estimation_base_image = est
        if self.estimation_updated_image is not None:
            self.do_have_estimate = True

    def set_updated_estimate(self, est):
        self.estimation_updated_image = est
        if self.estimation_base_image is not None:
            self.do_have_estimate = True

    def __getitem__(self, index):
        patch_id = int(self.patchs[index][0])
        rect = np.array(self.patchs[index][1]['rect'])
        msize = self.patchs[index][1]['size']

        ## applying scale to rect:
        rect = np.round(rect * self.scale)
        rect = rect.astype('int')
        msize = round(msize * self.scale)

        patch_rgb = impatch(self.rgb_image, rect)
        if self.do_have_estimate:
            patch_whole_estimate_base = impatch(self.estimation_base_image, rect)
            patch_whole_estimate_updated = impatch(self.estimation_updated_image, rect)
            return {'patch_rgb': patch_rgb, 'patch_whole_estimate_base': patch_whole_estimate_base,
                    'patch_whole_estimate_updated': patch_whole_estimate_updated, 'rect': rect,
                    'size': msize, 'id': patch_id}
        else:
            return {'patch_rgb': patch_rgb, 'rect': rect, 'size': msize, 'id': patch_id}

class MyBaseOptions():
    """This class defines options used during both training and test time.

    It also implements several helper functions such as parsing, printing, and saving the options.
    It also gathers additional options defined in <modify_commandline_options> functions in both dataset class and model class.
    """

    def __init__(self):
        """Reset the class; indicates the class hasn't been initailized"""
        self.initialized = False

    def initialize(self, parser):
        """Define the common options that are used in both training and test."""
        # basic parameters
        parser.add_argument('--dataroot', help='path to images (should have subfolders trainA, trainB, valA, valB, etc)')
        parser.add_argument('--name', type=str, default='void', help='mahdi_unet_new, scaled_unet')
        parser.add_argument('--gpu_ids', type=str, default='0', help='gpu ids: e.g. 0  0,1,2, 0,2. use -1 for CPU')
        parser.add_argument('--checkpoints_dir', type=str, default='./pix2pix/checkpoints', help='models are saved here')
        # model parameters
        parser.add_argument('--model', type=str, default='cycle_gan', help='chooses which model to use. [cycle_gan | pix2pix | test | colorization]')
        parser.add_argument('--input_nc', type=int, default=2, help='# of input image channels: 3 for RGB and 1 for grayscale')
        parser.add_argument('--output_nc', type=int, default=1, help='# of output image channels: 3 for RGB and 1 for grayscale')
        parser.add_argument('--ngf', type=int, default=64, help='# of gen filters in the last conv layer')
        parser.add_argument('--ndf', type=int, default=64, help='# of discrim filters in the first conv layer')
        parser.add_argument('--netD', type=str, default='basic', help='specify discriminator architecture [basic | n_layers | pixel]. The basic model is a 70x70 PatchGAN. n_layers allows you to specify the layers in the discriminator')
        parser.add_argument('--netG', type=str, default='resnet_9blocks', help='specify generator architecture [resnet_9blocks | resnet_6blocks | unet_256 | unet_128]')
        parser.add_argument('--n_layers_D', type=int, default=3, help='only used if netD==n_layers')
        parser.add_argument('--norm', type=str, default='instance', help='instance normalization or batch normalization [instance | batch | none]')
        parser.add_argument('--init_type', type=str, default='normal', help='network initialization [normal | xavier | kaiming | orthogonal]')
        parser.add_argument('--init_gain', type=float, default=0.02, help='scaling factor for normal, xavier and orthogonal.')
        parser.add_argument('--no_dropout', action='store_true', help='no dropout for the generator')
        # dataset parameters
        parser.add_argument('--dataset_mode', type=str, default='unaligned', help='chooses how datasets are loaded. [unaligned | aligned | single | colorization]')
        parser.add_argument('--direction', type=str, default='AtoB', help='AtoB or BtoA')
        parser.add_argument('--serial_batches', action='store_true', help='if true, takes images in order to make batches, otherwise takes them randomly')
        parser.add_argument('--num_threads', default=4, type=int, help='# threads for loading data')
        parser.add_argument('--batch_size', type=int, default=1, help='input batch size')
        parser.add_argument('--load_size', type=int, default=672, help='scale images to this size')
        parser.add_argument('--crop_size', type=int, default=672, help='then crop to this size')
        parser.add_argument('--max_dataset_size', type=int, default=10000, help='Maximum number of samples allowed per dataset. If the dataset directory contains more than max_dataset_size, only a subset is loaded.')
        parser.add_argument('--preprocess', type=str, default='resize_and_crop', help='scaling and cropping of images at load time [resize_and_crop | crop | scale_width | scale_width_and_crop | none]')
        parser.add_argument('--no_flip', action='store_true', help='if specified, do not flip the images for data augmentation')
        parser.add_argument('--display_winsize', type=int, default=256, help='display window size for both visdom and HTML')
        # additional parameters
        parser.add_argument('--epoch', type=str, default='latest', help='which epoch to load? set to latest to use latest cached model')
        parser.add_argument('--load_iter', type=int, default='0', help='which iteration to load? if load_iter > 0, the code will load models by iter_[load_iter]; otherwise, the code will load models by [epoch]')
        parser.add_argument('--verbose', action='store_true', help='if specified, print more debugging information')
        parser.add_argument('--suffix', default='', type=str, help='customized suffix: opt.name = opt.name + suffix: e.g., {model}_{netG}_size{load_size}')

        parser.add_argument('--data_dir', type=str, required=False,
                            help='input files directory images can be .png .jpg .tiff')
        parser.add_argument('--output_dir', type=str, required=False,
                            help='result dir. result depth will be png. vides are JMPG as avi')
        parser.add_argument('--savecrops', type=int, required=False)
        parser.add_argument('--savewholeest', type=int, required=False)
        parser.add_argument('--output_resolution', type=int, required=False,
                            help='0 for no restriction 1 for resize to input size')
        parser.add_argument('--net_receptive_field_size', type=int, required=False)
        parser.add_argument('--pix2pixsize', type=int, required=False)
        parser.add_argument('--generatevideo', type=int, required=False)
        parser.add_argument('--depthNet', type=int, required=False, help='0: midas 1:strurturedRL')
        parser.add_argument('--R0', action='store_true')
        parser.add_argument('--R20', action='store_true')
        parser.add_argument('--Final', action='store_true')
        parser.add_argument('--colorize_results', action='store_true')
        parser.add_argument('--max_res', type=float, default=np.inf)

        self.initialized = True
        return parser

    def gather_options(self):
        """Initialize our parser with basic options(only once).
        Add additional model-specific and dataset-specific options.
        These options are defined in the <modify_commandline_options> function
        in model and dataset classes.
        """
        if not self.initialized:  # check if it has been initialized
            parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
            parser = self.initialize(parser)

        # get the basic options
        opt, _ = parser.parse_known_args()

        # modify model-related parser options
        model_name = opt.model
        model_option_setter = pix2pix.models.get_option_setter(model_name)
        parser = model_option_setter(parser, self.isTrain)
        opt, _ = parser.parse_known_args()  # parse again with new defaults

        # modify dataset-related parser options
        dataset_name = opt.dataset_mode
        dataset_option_setter = pix2pix.data.get_option_setter(dataset_name)
        parser = dataset_option_setter(parser, self.isTrain)

        # save and return the parser
        self.parser = parser
        #return parser.parse_args() #EVIL
        return opt

    def print_options(self, opt):
        """Print and save options

        It will print both current options and default values(if different).
        It will save options into a text file / [checkpoints_dir] / opt.txt
        """
        message = ''
        message += '----------------- Options ---------------\n'
        for k, v in sorted(vars(opt).items()):
            comment = ''
            default = self.parser.get_default(k)
            if v != default:
                comment = '\t[default: %s]' % str(default)
            message += '{:>25}: {:<30}{}\n'.format(str(k), str(v), comment)
        message += '----------------- End -------------------'
        print(message)

        # save to the disk
        expr_dir = os.path.join(opt.checkpoints_dir, opt.name)
        util.mkdirs(expr_dir)
        file_name = os.path.join(expr_dir, '{}_opt.txt'.format(opt.phase))
        with open(file_name, 'wt') as opt_file:
            opt_file.write(message)
            opt_file.write('\n')

    def parse(self):
        """Parse our options, create checkpoints directory suffix, and set up gpu device."""
        opt = self.gather_options()
        opt.isTrain = self.isTrain   # train or test

        # process opt.suffix
        if opt.suffix:
            suffix = ('_' + opt.suffix.format(**vars(opt))) if opt.suffix != '' else ''
            opt.name = opt.name + suffix

        #self.print_options(opt)

        # set gpu ids
        str_ids = opt.gpu_ids.split(',')
        opt.gpu_ids = []
        for str_id in str_ids:
            id = int(str_id)
            if id >= 0:
                opt.gpu_ids.append(id)
        #if len(opt.gpu_ids) > 0:
        #    torch.cuda.set_device(opt.gpu_ids[0])

        self.opt = opt
        return self.opt

class MyTestOptions(MyBaseOptions):
    """This class includes test options.

    It also includes shared options defined in BaseOptions.
    """

    def initialize(self, parser):
        parser = MyBaseOptions.initialize(self, parser)  # define shared options
        parser.add_argument('--aspect_ratio', type=float, default=1.0, help='aspect ratio of result images')
        parser.add_argument('--phase', type=str, default='test', help='train, val, test, etc')
        # Dropout and Batchnorm has different behavioir during training and test.
        parser.add_argument('--eval', action='store_true', help='use eval mode during test time.')
        parser.add_argument('--num_test', type=int, default=50, help='how many test images to run')
        # rewrite devalue values
        parser.set_defaults(model='pix2pix4depth')
        # To avoid cropping, the load_size should be the same as crop_size
        parser.set_defaults(load_size=parser.get_default('crop_size'))
        self.isTrain = False
        return parser

def estimateboost(img, model, model_type, pix2pixmodel):
	# get settings
	if hasattr(opts, 'depthmap_script_boost_rmax'):
		whole_size_threshold = opts.depthmap_script_boost_rmax
		
	if model_type == 0: #leres
		net_receptive_field_size = 448
		patch_netsize = 2 * net_receptive_field_size
	elif model_type == 1: #dpt_beit_large_512
		net_receptive_field_size = 512
		patch_netsize = 2 * net_receptive_field_size
	else: #other midas
		net_receptive_field_size = 384
		patch_netsize = 2 * net_receptive_field_size

	gc.collect()
	devices.torch_gc()

	# Generate mask used to smoothly blend the local pathc estimations to the base estimate.
	# It is arbitrarily large to avoid artifacts during rescaling for each crop.
	mask_org = generatemask((3000, 3000))
	mask = mask_org.copy()

	# Value x of R_x defined in the section 5 of the main paper.
	r_threshold_value = 0.2
	#if R0:
	#	r_threshold_value = 0

	input_resolution = img.shape
	scale_threshold = 3  # Allows up-scaling with a scale up to 3

	# Find the best input resolution R-x. The resolution search described in section 5-double estimation of the main paper and section B of the
	# supplementary material.
	whole_image_optimal_size, patch_scale = calculateprocessingres(img, net_receptive_field_size, r_threshold_value, scale_threshold, whole_size_threshold)

	print('wholeImage being processed in :', whole_image_optimal_size)

	# Generate the base estimate using the double estimation.
	whole_estimate = doubleestimate(img, net_receptive_field_size, whole_image_optimal_size, pix2pixsize, model, model_type, pix2pixmodel)

	# Compute the multiplier described in section 6 of the main paper to make sure our initial patch can select
	# small high-density regions of the image.
	global factor
	factor = max(min(1, 4 * patch_scale * whole_image_optimal_size / whole_size_threshold), 0.2)
	print('Adjust factor is:', 1/factor)

	# Compute the default target resolution.
	if img.shape[0] > img.shape[1]:
		a = 2 * whole_image_optimal_size
		b = round(2 * whole_image_optimal_size * img.shape[1] / img.shape[0])
	else:
		a = round(2 * whole_image_optimal_size * img.shape[0] / img.shape[1])
		b = 2 * whole_image_optimal_size
	b = int(round(b / factor))
	a = int(round(a / factor))

	"""
	# recompute a, b and saturate to max res.
	if max(a,b) > max_res:
		print('Default Res is higher than max-res: Reducing final resolution')
		if img.shape[0] > img.shape[1]:
			a = max_res
			b = round(option.max_res * img.shape[1] / img.shape[0])
		else:
			a = round(option.max_res * img.shape[0] / img.shape[1])
			b = max_res
		b = int(b)
		a = int(a)
	"""

	img = cv2.resize(img, (b, a), interpolation=cv2.INTER_CUBIC)

	# Extract selected patches for local refinement
	base_size = net_receptive_field_size * 2
	patchset = generatepatchs(img, base_size)

	print('Target resolution: ', img.shape)

	# Computing a scale in case user prompted to generate the results as the same resolution of the input.
	# Notice that our method output resolution is independent of the input resolution and this parameter will only
	# enable a scaling operation during the local patch merge implementation to generate results with the same resolution
	# as the input.
	"""
	if output_resolution == 1:
		mergein_scale = input_resolution[0] / img.shape[0]
		print('Dynamicly change merged-in resolution; scale:', mergein_scale)
	else:
		mergein_scale = 1
	"""
	# always rescale to input res for now
	mergein_scale = input_resolution[0] / img.shape[0]

	imageandpatchs = ImageandPatchs('', '', patchset, img, mergein_scale)
	whole_estimate_resized = cv2.resize(whole_estimate, (round(img.shape[1]*mergein_scale),
										round(img.shape[0]*mergein_scale)), interpolation=cv2.INTER_CUBIC)
	imageandpatchs.set_base_estimate(whole_estimate_resized.copy())
	imageandpatchs.set_updated_estimate(whole_estimate_resized.copy())

	print('Resulting depthmap resolution will be :', whole_estimate_resized.shape[:2])
	print('patches to process: '+str(len(imageandpatchs)))

	# Enumerate through all patches, generate their estimations and refining the base estimate.
	for patch_ind in range(len(imageandpatchs)):
		
		# Get patch information
		patch = imageandpatchs[patch_ind] # patch object
		patch_rgb = patch['patch_rgb'] # rgb patch
		patch_whole_estimate_base = patch['patch_whole_estimate_base'] # corresponding patch from base
		rect = patch['rect'] # patch size and location
		patch_id = patch['id'] # patch ID
		org_size = patch_whole_estimate_base.shape # the original size from the unscaled input
		print('\t processing patch', patch_ind, '/', len(imageandpatchs)-1, '|', rect)

		# We apply double estimation for patches. The high resolution value is fixed to twice the receptive
		# field size of the network for patches to accelerate the process.
		patch_estimation = doubleestimate(patch_rgb, net_receptive_field_size, patch_netsize, pix2pixsize, model, model_type, pix2pixmodel)
		patch_estimation = cv2.resize(patch_estimation, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)
		patch_whole_estimate_base = cv2.resize(patch_whole_estimate_base, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)

		# Merging the patch estimation into the base estimate using our merge network:
		# We feed the patch estimation and the same region from the updated base estimate to the merge network
		# to generate the target estimate for the corresponding region.
		pix2pixmodel.set_input(patch_whole_estimate_base, patch_estimation)

		# Run merging network
		pix2pixmodel.test()
		visuals = pix2pixmodel.get_current_visuals()

		prediction_mapped = visuals['fake_B']
		prediction_mapped = (prediction_mapped+1)/2
		prediction_mapped = prediction_mapped.squeeze().cpu().numpy()

		mapped = prediction_mapped

		# We use a simple linear polynomial to make sure the result of the merge network would match the values of
		# base estimate
		p_coef = np.polyfit(mapped.reshape(-1), patch_whole_estimate_base.reshape(-1), deg=1)
		merged = np.polyval(p_coef, mapped.reshape(-1)).reshape(mapped.shape)

		merged = cv2.resize(merged, (org_size[1],org_size[0]), interpolation=cv2.INTER_CUBIC)

		# Get patch size and location
		w1 = rect[0]
		h1 = rect[1]
		w2 = w1 + rect[2]
		h2 = h1 + rect[3]

		# To speed up the implementation, we only generate the Gaussian mask once with a sufficiently large size
		# and resize it to our needed size while merging the patches.
		if mask.shape != org_size:
			mask = cv2.resize(mask_org, (org_size[1],org_size[0]), interpolation=cv2.INTER_LINEAR)

		tobemergedto = imageandpatchs.estimation_updated_image

		# Update the whole estimation:
		# We use a simple Gaussian mask to blend the merged patch region with the base estimate to ensure seamless
		# blending at the boundaries of the patch region.
		tobemergedto[h1:h2, w1:w2] = np.multiply(tobemergedto[h1:h2, w1:w2], 1 - mask) + np.multiply(merged, mask)
		imageandpatchs.set_updated_estimate(tobemergedto)

	# output
	return cv2.resize(imageandpatchs.estimation_updated_image, (input_resolution[1], input_resolution[0]), interpolation=cv2.INTER_CUBIC)

# taken from 3d-photo-inpainting and modified
def sparse_bilateral_filtering(
    depth, image, filter_size, depth_threshold, sigma_s, sigma_r, HR=False, mask=None, gsHR=True, edge_id=None, num_iter=None, num_gs_iter=None, spdb=False
):
    save_images = []
    save_depths = []
    save_discontinuities = []
    vis_depth = depth.copy()

    vis_image = image.copy()
    for i in range(num_iter):
        if isinstance(filter_size, list):
            window_size = filter_size[i]
        else:
            window_size = filter_size
        vis_image = image.copy()
        save_images.append(vis_image)
        save_depths.append(vis_depth)
        u_over, b_over, l_over, r_over = vis_depth_discontinuity(vis_depth, depth_threshold, mask=mask) # test label true
        vis_image[u_over > 0] = np.array([0, 0, 0])
        vis_image[b_over > 0] = np.array([0, 0, 0])
        vis_image[l_over > 0] = np.array([0, 0, 0])
        vis_image[r_over > 0] = np.array([0, 0, 0])

        discontinuity_map = (u_over + b_over + l_over + r_over).clip(0.0, 1.0)
        discontinuity_map[depth == 0] = 1
        save_discontinuities.append(discontinuity_map)
        if mask is not None:
            discontinuity_map[mask == 0] = 0
        vis_depth = bilateral_filter(
            vis_depth, filter_size, sigma_s, sigma_r, discontinuity_map=discontinuity_map, HR=HR, mask=mask, window_size=window_size
        )

    return save_images, save_depths

def vis_depth_discontinuity(depth, depth_threshold, vis_diff=False, label=False, mask=None):
    """
    config:
    -
    """
    if label == False:
        disp = 1./depth
        u_diff = (disp[1:, :] - disp[:-1, :])[:-1, 1:-1]
        b_diff = (disp[:-1, :] - disp[1:, :])[1:, 1:-1]
        l_diff = (disp[:, 1:] - disp[:, :-1])[1:-1, :-1]
        r_diff = (disp[:, :-1] - disp[:, 1:])[1:-1, 1:]
        if mask is not None:
            u_mask = (mask[1:, :] * mask[:-1, :])[:-1, 1:-1]
            b_mask = (mask[:-1, :] * mask[1:, :])[1:, 1:-1]
            l_mask = (mask[:, 1:] * mask[:, :-1])[1:-1, :-1]
            r_mask = (mask[:, :-1] * mask[:, 1:])[1:-1, 1:]
            u_diff = u_diff * u_mask
            b_diff = b_diff * b_mask
            l_diff = l_diff * l_mask
            r_diff = r_diff * r_mask
        u_over = (np.abs(u_diff) > depth_threshold).astype(np.float32)
        b_over = (np.abs(b_diff) > depth_threshold).astype(np.float32)
        l_over = (np.abs(l_diff) > depth_threshold).astype(np.float32)
        r_over = (np.abs(r_diff) > depth_threshold).astype(np.float32)
    else:
        disp = depth
        u_diff = (disp[1:, :] * disp[:-1, :])[:-1, 1:-1]
        b_diff = (disp[:-1, :] * disp[1:, :])[1:, 1:-1]
        l_diff = (disp[:, 1:] * disp[:, :-1])[1:-1, :-1]
        r_diff = (disp[:, :-1] * disp[:, 1:])[1:-1, 1:]
        if mask is not None:
            u_mask = (mask[1:, :] * mask[:-1, :])[:-1, 1:-1]
            b_mask = (mask[:-1, :] * mask[1:, :])[1:, 1:-1]
            l_mask = (mask[:, 1:] * mask[:, :-1])[1:-1, :-1]
            r_mask = (mask[:, :-1] * mask[:, 1:])[1:-1, 1:]
            u_diff = u_diff * u_mask
            b_diff = b_diff * b_mask
            l_diff = l_diff * l_mask
            r_diff = r_diff * r_mask
        u_over = (np.abs(u_diff) > 0).astype(np.float32)
        b_over = (np.abs(b_diff) > 0).astype(np.float32)
        l_over = (np.abs(l_diff) > 0).astype(np.float32)
        r_over = (np.abs(r_diff) > 0).astype(np.float32)
    u_over = np.pad(u_over, 1, mode='constant')
    b_over = np.pad(b_over, 1, mode='constant')
    l_over = np.pad(l_over, 1, mode='constant')
    r_over = np.pad(r_over, 1, mode='constant')
    u_diff = np.pad(u_diff, 1, mode='constant')
    b_diff = np.pad(b_diff, 1, mode='constant')
    l_diff = np.pad(l_diff, 1, mode='constant')
    r_diff = np.pad(r_diff, 1, mode='constant')

    if vis_diff:
        return [u_over, b_over, l_over, r_over], [u_diff, b_diff, l_diff, r_diff]
    else:
        return [u_over, b_over, l_over, r_over]

def bilateral_filter(depth, filter_size, sigma_s, sigma_r, discontinuity_map=None, HR=False, mask=None, window_size=False):
    #sigma_s = config['sigma_s']
    #sigma_r = config['sigma_r']
    if window_size == False:
        window_size = filter_size
    midpt = window_size//2
    ax = np.arange(-midpt, midpt+1.)
    xx, yy = np.meshgrid(ax, ax)
    if discontinuity_map is not None:
        spatial_term = np.exp(-(xx**2 + yy**2) / (2. * sigma_s**2))

    # padding
    depth = depth[1:-1, 1:-1]
    depth = np.pad(depth, ((1,1), (1,1)), 'edge')
    pad_depth = np.pad(depth, (midpt,midpt), 'edge')
    if discontinuity_map is not None:
        discontinuity_map = discontinuity_map[1:-1, 1:-1]
        discontinuity_map = np.pad(discontinuity_map, ((1,1), (1,1)), 'edge')
        pad_discontinuity_map = np.pad(discontinuity_map, (midpt,midpt), 'edge')
        pad_discontinuity_hole = 1 - pad_discontinuity_map
    # filtering
    output = depth.copy()
    pad_depth_patches = rolling_window(pad_depth, [window_size, window_size], [1,1])
    if discontinuity_map is not None:
        pad_discontinuity_patches = rolling_window(pad_discontinuity_map, [window_size, window_size], [1,1])
        pad_discontinuity_hole_patches = rolling_window(pad_discontinuity_hole, [window_size, window_size], [1,1])

    if mask is not None:
        pad_mask = np.pad(mask, (midpt,midpt), 'constant')
        pad_mask_patches = rolling_window(pad_mask, [window_size, window_size], [1,1])
    from itertools import product
    if discontinuity_map is not None:
        pH, pW = pad_depth_patches.shape[:2]
        for pi in range(pH):
            for pj in range(pW):
                if mask is not None and mask[pi, pj] == 0:
                    continue
                if discontinuity_map is not None:
                    if bool(pad_discontinuity_patches[pi, pj].any()) is False:
                        continue
                    discontinuity_patch = pad_discontinuity_patches[pi, pj]
                    discontinuity_holes = pad_discontinuity_hole_patches[pi, pj]
                depth_patch = pad_depth_patches[pi, pj]
                depth_order = depth_patch.ravel().argsort()
                patch_midpt = depth_patch[window_size//2, window_size//2]
                if discontinuity_map is not None:
                    coef = discontinuity_holes.astype(np.float32)
                    if mask is not None:
                        coef = coef * pad_mask_patches[pi, pj]
                else:
                    range_term = np.exp(-(depth_patch-patch_midpt)**2 / (2. * sigma_r**2))
                    coef = spatial_term * range_term
                if coef.max() == 0:
                    output[pi, pj] = patch_midpt
                    continue
                if discontinuity_map is not None and (coef.max() == 0):
                    output[pi, pj] = patch_midpt
                else:
                    coef = coef/(coef.sum())
                    coef_order = coef.ravel()[depth_order]
                    cum_coef = np.cumsum(coef_order)
                    ind = np.digitize(0.5, cum_coef)
                    output[pi, pj] = depth_patch.ravel()[depth_order][ind]
    else:
        pH, pW = pad_depth_patches.shape[:2]
        for pi in range(pH):
            for pj in range(pW):
                if discontinuity_map is not None:
                    if pad_discontinuity_patches[pi, pj][window_size//2, window_size//2] == 1:
                        continue
                    discontinuity_patch = pad_discontinuity_patches[pi, pj]
                    discontinuity_holes = (1. - discontinuity_patch)
                depth_patch = pad_depth_patches[pi, pj]
                depth_order = depth_patch.ravel().argsort()
                patch_midpt = depth_patch[window_size//2, window_size//2]
                range_term = np.exp(-(depth_patch-patch_midpt)**2 / (2. * sigma_r**2))
                if discontinuity_map is not None:
                    coef = spatial_term * range_term * discontinuity_holes
                else:
                    coef = spatial_term * range_term
                if coef.sum() == 0:
                    output[pi, pj] = patch_midpt
                    continue
                if discontinuity_map is not None and (coef.sum() == 0):
                    output[pi, pj] = patch_midpt
                else:
                    coef = coef/(coef.sum())
                    coef_order = coef.ravel()[depth_order]
                    cum_coef = np.cumsum(coef_order)
                    ind = np.digitize(0.5, cum_coef)
                    output[pi, pj] = depth_patch.ravel()[depth_order][ind]

    return output

def rolling_window(a, window, strides):
    assert len(a.shape)==len(window)==len(strides), "\'a\', \'window\', \'strides\' dimension mismatch"
    shape_fn = lambda i,w,s: (a.shape[i]-w)//s + 1
    shape = [shape_fn(i,w,s) for i,(w,s) in enumerate(zip(window, strides))] + list(window)
    def acc_shape(i):
        if i+1>=len(a.shape):
            return 1
        else:
            return reduce(lambda x,y:x*y, a.shape[i+1:])
    _strides = [acc_shape(i)*s*a.itemsize for i,s in enumerate(strides)] + list(a.strides)

    return np.lib.stride_tricks.as_strided(a, shape=shape, strides=_strides)
