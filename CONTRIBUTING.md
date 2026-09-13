# Contributing

Thanks for helping move Character Creator forward. The project is experimental, and the most valuable contributions are reproducible improvements to small-part segmentation, hidden-part reconstruction, multi-angle consistency, and Adobe Character Animator PSD validation.

## Before opening a pull request

1. Open an issue describing the failure, model combination, canvas size, GPU/VRAM, and exact steps to reproduce it.
2. Keep generated artwork, model files, local projects, logs, and `config.json` out of commits.
3. Add or update tests when changing project state, workflow construction, queue behavior, layer naming, or PSD export.
4. Run `python -m unittest -v test_creator`.
5. Explain the visible behavior change and any models or custom nodes required.

Small focused pull requests are easiest to review. Do not include copyrighted characters or model weights. By contributing, you agree that your contribution is licensed under the MIT License.
