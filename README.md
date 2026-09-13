# Character Creator for ComfyUI

Character Creator is an experimental Windows desktop application for turning a character reference into separated, aligned artwork and a layered PSD organized for Adobe Character Animator.

> **Development status:** this is an unfinished prototype. Large body regions can work, but small features such as eyes, pupils, mouths, hands, hidden anatomy, and clean attachment overlaps are not reliable. Review every generated layer. See [Project status](PROJECT_STATUS.md) before using it for production.

## What it does

- Creates Front, Quarter, and Side character sections on a shared canvas.
- Supports viseme mouth artwork or a Nutcracker Jaw layout.
- Uses ComfyUI for initial reference generation, image editing, and SAM3 segmentation.
- Supports SDXL, SDXL-Lightning, Qwen Image Edit, Flux.2 Klein, compatible VAEs, and LoRA stacks through configurable workflows.
- Renders missing artwork in batches with per-piece progress and status colors.
- Lets you review mask candidates and approve finished pieces while a batch continues.
- Shows the live ComfyUI queue and can interrupt running jobs or remove waiting jobs.
- Exports a layered PSD with Adobe Character Animator-style names, plus a preview and rigging notes.

## What it does not solve

The current pipeline cannot guarantee consistent identity, exact alignment, complete transparent cutouts, or usable small facial and hand components. It exports artwork, not a finished `.puppet`; behaviors, handles, tags, pivots, triggers, and calibration still need verification in Adobe Character Animator.

## Requirements

- Windows and Python 3.11 or newer.
- A local [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installation.
- Model files compatible with the selected workflow.
- [ComfyUI-SAM3](https://github.com/1038lab/ComfyUI-SAM3) and its SAM3 model for automatic separation.
- Enough VRAM/RAM for the selected generation and editing models.

Model weights are not included in this repository and retain their own licenses.

## Install

1. Clone this repository.
2. Run `setup.cmd`.
3. Open `config.json` and set `comfy_root` to your ComfyUI directory. The app creates this file from `config.example.json` during setup or first launch.
4. Install the model files and custom nodes required by your selected workflows.
5. Run `Launch Character Creator.cmd`.
6. Click **Start ComfyUI**, then **Check ComfyUI**.

The optional `install_fast_models.py` downloader installs the official SDXL-Lightning 4-step checkpoint and Flux.2 Klein 4B files into the configured ComfyUI directory. These are large downloads. Review the model licenses and storage requirements before running it.

## Basic workflow

1. Create a character, enter its description, select Front/Quarter/Side sections, and choose visemes or jaw mode.
2. Generate or import the Front reference and approve it.
3. Generate and approve the reference for each enabled turned view.
4. Render or extract individual parts. **Start batch** processes missing parts for checked sections.
5. Review detected masks, transparency, seams, identity, and placement. Manual PNG replacement is supported.
6. Export a reviewed PSD or an explicitly incomplete draft PSD.
7. Import the PSD into Adobe Character Animator and finish rigging there.

The character description can be edited later with **Edit prompt**. **Models & LoRAs** reads choices from the running ComfyUI server. Model families must use compatible encoders and VAEs; the app validates known Qwen/Klein combinations before submission.

## Batch review and queue control

Artwork rows show whether a piece is missing, queued, rendering, masking, waiting for mask selection, waiting for review, approved, stopped, or failed. Finished masks can be selected and approved while the next piece is rendering.

Open **ComfyUI queue / Stop jobs** to see all running and waiting jobs. Cancellation is cooperative: ComfyUI may need time to leave a GPU operation. Stopping a job does not kill or restart ComfyUI.

## PSD structure

The exporter creates independent `Body` and `+Head` hierarchies with Adobe view names such as `Frontal`, `Left Quarter`, and `Left Profile`. It includes separate body parts, face features, eye groups, blinks, and either viseme mouth layers or a movable jaw. The first enabled view is visible in the resting composite; alternate views and facial swaps are hidden.

Adobe’s naming reference: [Prepare artwork for Adobe Character Animator](https://helpx.adobe.com/adobe-character-animator/desktop/creating-and-controlling-puppets/prepare-artwork.html).

## Tests

```powershell
python -m unittest -v test_creator
```

The test suite covers project structure, alpha handling, masks, placement, PSD hierarchy, export validation, model/workflow pairing, batch behavior, progress reporting, concurrent review, and queue cancellation. GPU integration checks require a configured ComfyUI installation and are not part of the default unit test run.

## Privacy and generated files

`projects/`, `config.json`, logs, generated images, PSD exports, model records, and local backups are ignored by Git. Check staged files before every contribution; generated artwork and local ComfyUI data can contain private material or machine paths.

## Contributing

The highest-value open problems are documented in [PROJECT_STATUS.md](PROJECT_STATUS.md). Please read [CONTRIBUTING.md](CONTRIBUTING.md) and do not upload model weights, credentials, private artwork, or copyrighted character assets.

## License

The application code is released under the [MIT License](LICENSE). Third-party models, custom nodes, ComfyUI, Adobe products, and generated assets are governed by their own terms.
