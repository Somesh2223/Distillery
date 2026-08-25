"""Packages a run's fetched items into an ML-ready dataset:
folder-per-split/per-label layout, metadata.csv + metadata.json manifests,
a COCO-style annotations.json for images, and a downloadable zip.
"""
from __future__ import annotations

import csv
import json
import random
import shutil
import zipfile
from pathlib import Path

from config import DATASETS_DIR, FETCHED_DIR
from logging_setup import get_logger, log_event
from models import ExportOptions
import storage

logger = get_logger(__name__)


def export_dataset(run_id: str, label: str, data_type: str, options: ExportOptions) -> Path:
    items = storage.list_items_for_run(run_id, offset=0, limit=1_000_000)
    if not items:
        raise ValueError("no items to export for this run")

    rng = random.Random(options.seed)
    shuffled = items[:]
    rng.shuffle(shuffled)

    ratios = options.split.normalized()
    n = len(shuffled)
    n_train = round(n * ratios.train)
    n_val = round(n * ratios.val)
    splits = (["train"] * n_train) + (["val"] * n_val)
    splits += ["test"] * (n - len(splits))

    dataset_dir = DATASETS_DIR / run_id
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)
    dataset_dir.mkdir(parents=True)

    manifest_rows: list[dict] = []
    coco_images: list[dict] = []
    coco_annotations: list[dict] = []
    category_id = 1

    for idx, (item, split) in enumerate(zip(shuffled, splits)):
        dest_rel = None
        src_path = FETCHED_DIR / item["local_path"] if item["local_path"] else None

        if item["data_type"] == "image" and src_path and src_path.exists():
            dest_rel = _place_image(src_path, dataset_dir, split, label, options)
            if dest_rel:
                abs_dest = dataset_dir / dest_rel
                w, h = _image_size(abs_dest)
                image_id = idx + 1
                coco_images.append({"id": image_id, "file_name": dest_rel, "width": w, "height": h})
                coco_annotations.append({"id": image_id, "image_id": image_id, "category_id": category_id})
        elif item["data_type"] in ("text", "structured") and src_path and src_path.exists():
            subdir = "text" if item["data_type"] == "text" else "structured"
            split_dir = dataset_dir / subdir / split / label
            split_dir.mkdir(parents=True, exist_ok=True)
            dest_path = split_dir / src_path.name
            shutil.copyfile(src_path, dest_path)
            dest_rel = str(dest_path.relative_to(dataset_dir)).replace("\\", "/")

        manifest_rows.append(
            {
                "id": item["id"],
                "split": split,
                "label": label,
                "data_type": item["data_type"],
                "source_name": item["source_name"],
                "source_url": item["source_url"],
                "dataset_path": dest_rel,
                "title": item["title"],
                "license": item["license"],
                "attribution": item["attribution"],
                "author": item["author"],
                "published_at": item["published_at"],
                "fetched_at": item["fetched_at"],
            }
        )

    _write_manifests(dataset_dir, manifest_rows)

    if data_type == "image" and coco_images:
        coco = {
            "images": coco_images,
            "annotations": coco_annotations,
            "categories": [{"id": category_id, "name": label}],
        }
        with open(dataset_dir / "annotations.json", "w", encoding="utf-8") as f:
            json.dump(coco, f, indent=2)

    zip_path = _zip_dataset(dataset_dir, run_id)
    storage.set_run_dataset_path(run_id, str(zip_path))
    log_event(logger, "dataset_exported", run_id=run_id, item_count=len(manifest_rows), zip_path=str(zip_path))
    return zip_path


def _place_image(src_path: Path, dataset_dir: Path, split: str, label: str, options: ExportOptions) -> str | None:
    split_dir = dataset_dir / "images" / split / label
    split_dir.mkdir(parents=True, exist_ok=True)
    dest_path = split_dir / src_path.name
    try:
        if options.resize:
            from PIL import Image

            with Image.open(src_path) as im:
                im.convert("RGB").resize(tuple(options.resize)).save(dest_path)
        else:
            shutil.copyfile(src_path, dest_path)
    except Exception as exc:
        log_event(logger, "image_placement_failed", level=30, src=str(src_path), error=str(exc))
        return None
    return str(dest_path.relative_to(dataset_dir)).replace("\\", "/")


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _write_manifests(dataset_dir: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(dataset_dir / "metadata.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with open(dataset_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


def _zip_dataset(dataset_dir: Path, run_id: str) -> Path:
    zip_path = DATASETS_DIR / f"{run_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in dataset_dir.rglob("*"):
            if file.is_file():
                zf.write(file, Path(run_id) / file.relative_to(dataset_dir))
    return zip_path
