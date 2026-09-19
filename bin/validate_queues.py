#!/usr/bin/env python3
"""Check queue settings before slurm_config.sh changes the running configuration."""

import argparse
from pathlib import Path
import re
import sys

import yaml


# This offline precheck recognizes VM families, not per-AD SMT capabilities.
# Terraform remains responsible for checking the actual ListShapes response.
# https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm
AMD_VM = re.compile(r"^VM\.(?:Standard|DenseIO)\.E[0-9]+\.")
ARM_VM = re.compile(r"^VM\.Standard\.A[0-9]+\.")


def validate_queues(document):
    errors = []
    if not isinstance(document, dict) or not isinstance(document.get("queues"), list):
        return ["queues must be a non-empty list"]
    if not document["queues"]:
        return ["queues must be a non-empty list"]

    keywords = {}
    for queue_index, queue in enumerate(document["queues"], 1):
        queue_location = "queue #{}".format(queue_index)
        if not isinstance(queue, dict):
            errors.append(queue_location + " must be a mapping")
            continue
        if not isinstance(queue.get("name"), str) or not queue["name"].strip():
            errors.append(queue_location + " requires a non-empty name")
        else:
            queue_location = "queue {!r}".format(queue["name"])
        instances = queue.get("instance_types")
        if not isinstance(instances, list) or not instances:
            errors.append(queue_location + ": instance_types must be a non-empty list")
            continue

        for instance_index, instance in enumerate(instances, 1):
            location = "{} / instance type #{}".format(queue_location, instance_index)
            if not isinstance(instance, dict):
                errors.append(location + " must be a mapping")
                continue
            if isinstance(instance.get("name"), str) and instance["name"].strip():
                location = "{} / instance type {!r}".format(queue_location, instance["name"])
            else:
                errors.append(location + ": name must be a non-empty string")

            keyword = instance.get("instance_keyword")
            if not isinstance(keyword, str) or not keyword.strip():
                errors.append(location + ": instance_keyword must be a non-empty string")
            elif keyword in keywords:
                errors.append("{}: duplicate instance_keyword {!r}; also used by {}".format(
                    location, keyword, keywords[keyword]))
            else:
                keywords[keyword] = location

            shape = instance.get("shape")
            if not isinstance(shape, str) or not shape.strip():
                errors.append(location + ": shape must be a non-empty string")
                continue

            ht = instance.get("hyperthreading")
            if not isinstance(ht, bool) and not (isinstance(ht, str) and ht in ("true", "false")):
                errors.append(location + ": hyperthreading must be true or false")
                continue
            if ht is True or ht == "true":
                continue
            if shape.startswith("VM.") and not (AMD_VM.match(shape) or ARM_VM.match(shape)):
                errors.append(
                    "{} (shape={}): hyperthreading=false is unsupported for this VM family. "
                    "VM HT Off is supported only on AMD VM shapes; Intel VM HT Off is unsupported. "
                    "Set hyperthreading: true or select a supported AMD VM shape.".format(location, shape)
                )
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queues_file", type=Path)
    args = parser.parse_args()
    try:
        with args.queues_file.open(encoding="utf-8") as source:
            document = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        print("Error: cannot read {}: {}".format(args.queues_file, exc), file=sys.stderr)
        return 1
    errors = validate_queues(document)
    for error in errors:
        print("Error: {}: {}".format(args.queues_file, error), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
