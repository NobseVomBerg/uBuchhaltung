# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Workflows binden fremde Actions nur über einen festen Commit ein.

Ein Tag wie ``@v4`` lässt sich vom Herausgeber der Action jederzeit auf anderen Code umsetzen – auch in dem Lauf, der
Image und Release schreiben darf. Ein Commit (40 Hex-Zeichen) nicht. Die Version steht als Kommentar dahinter.
"""
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(#.*)?$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION = re.compile(r"^#\s*v\d+\.\d+\.\d+$")


def _uses():
    found = []
    for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                match = USES.match(line)
                if match:
                    found.append((os.path.basename(path), number, match.group(1), (match.group(2) or "").strip()))
    return found


def test_the_workflows_use_actions():
    assert len(_uses()) >= 10                       # die Suche findet die Zeilen wirklich


def test_every_action_is_pinned_to_a_commit_with_its_version_as_a_comment():
    loose = [f"{name}:{number} {target} {comment}" for name, number, target, comment in _uses()
             if not target.startswith("./") and not (PINNED.match(target) and VERSION.match(comment))]
    assert loose == []


def test_one_action_has_one_commit_in_all_workflows():
    commits = {}
    for _name, _number, target, _comment in _uses():
        action, _, commit = target.partition("@")
        commits.setdefault(action, set()).add(commit)
    assert {action: sorted(found) for action, found in commits.items() if len(found) > 1} == {}
