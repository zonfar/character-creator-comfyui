# Project status and open problems

Character Creator is an experimental Windows desktop application, not a finished product. It can create a character project, submit generation and segmentation workflows to ComfyUI, review masks, track the queue, and export a layered PSD using Adobe Character Animator naming conventions.

The central unresolved problem is reliable production of small, animation-ready parts. Eyes, pupils, eyebrows, mouths, hands, occluded anatomy, and clean attachment overlaps can be missed, merged, altered, or invented by the current model and segmentation pipeline. Cross-angle identity and pixel alignment are also probabilistic. Human review and replacement artwork remain necessary.

## High-value directions

- Replace prompt-only segmentation with landmark-aware or pose-guided extraction.
- Generate facial features at higher local resolution, then transform them back into the shared canvas.
- Add deterministic eye, mouth, and hand crops with per-part segmentation models.
- Represent occluded anatomy explicitly and reconstruct only attachment overlap regions.
- Add ControlNet, pose, depth, or correspondence constraints across angles.
- Build repeatable evaluation fixtures for seams, alpha coverage, alignment, identity, and Adobe layer structure.
- Validate exported PSDs in Adobe Character Animator and document the required behaviors, handles, and tags.
- Support more ComfyUI model families through versioned workflow adapters instead of filename-based assumptions.

This repository is open so contributors can test those ideas, replace weak parts of the pipeline, and carry the concept farther than its original prototype.
