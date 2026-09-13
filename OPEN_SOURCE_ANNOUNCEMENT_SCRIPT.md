# I’m Open-Sourcing Character Creator

I started Character Creator because I wanted to build a product that could turn one AI-generated character into an animation-ready Photoshop file.

The idea was ambitious: generate matching front, quarter, and side views; separate the head and body; isolate the eyes, pupils, eyebrows, mouth shapes, arms, legs, and hands; then organize everything using Adobe Character Animator’s layer naming conventions.

I wanted to sell it as a product. I spent time building the desktop interface, ComfyUI workflows, batch rendering, mask review, model controls, queue management, and PSD export. Some of it works. It can generate references, attempt matching angles and parts, preserve a shared canvas, and build a layered PSD.

But I hit a brick wall where the project matters most. The current image models and segmentation pipeline are not reliable enough with tiny details like eyes, pupils, hands, mouth shapes, hidden anatomy, and clean attachment overlaps. A tool meant to save production time cannot quietly produce broken puppet parts and call the job finished.

So I’m open-sourcing it.

This is unfinished software. It is a working experiment and a starting point, not a polished product. I hope someone with experience in computer vision, pose guidance, segmentation, image correspondence, ComfyUI, or Adobe Character Animator can take pieces of it, improve it, or find an approach I missed.

I also want to make this a broader rule for my work: when I build something with real potential but hit a wall that keeps it from becoming a product, I would rather open-source it than leave it buried on a hard drive. Unfinished projects like this can still be useful. Someone else may have the missing idea, the right expertise, or simply the energy to keep going.

If you want to help, the biggest challenges are small-part extraction, consistent geometry across angles, hidden-part reconstruction, and repeatable validation of the final Character Animator rig.

The source is available on GitHub under the MIT License. Fork it, break it, improve it, and take it farther.
