"""Headless CLI runner — useful for batch / scripting / smoke-tests.

Usage:
  python cli.py sample_product.json
"""
import sys
import json
import generator as G

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cli.py <product.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        spec = json.load(f)
    res = G.generate_pack(
        product=spec["product"],
        formats_selected=spec.get("formats", ["wb_main"]),
        card_plan=spec.get("card_plan", {"main": 1, "feature": 3}),
        style_key=spec.get("style", "minimal"),
        ref_paths=spec.get("photos", []),
        palette_override=spec.get("palette"),
        progress_cb=lambda m: print(m),
    )
    print(f"\n✅ {res['run_dir']} ({len(res['files'])} карточек)")
