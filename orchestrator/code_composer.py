"""
Code Composer — merges extracted skill code with new LLM-generated code.

Used by the Dev agent in 'exploit' mode to compose a final solution from:
  1. Imports deduplicated across all contributing skill files
  2. Helper functions extracted from skill files
  3. The new LLM-generated code

Falls back to returning new_code unchanged if any parsing error occurs.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

from memory.skill import Skill

LOGGER = logging.getLogger(__name__)


class CodeComposer:
    """
    Compose final solution code from skill snippets + new LLM code.

    Example
    -------
    composer = CodeComposer()
    final = composer.compose(relevant_skills, raw_llm_code)
    """

    def compose(self, skills: list[Skill], new_code: str) -> str:
        """
        Combine skill helper code with new_code into a single module.

        Returns new_code unchanged if skills is empty or if any error occurs.
        """
        if not skills:
            return new_code

        try:
            imports = self._extract_imports(skills)
            skill_body = self._merge_skill_functions(skills)
            deduped = self._deduplicate_imports(imports)

            parts: list[str] = []
            if deduped:
                parts.append(deduped)
            if skill_body:
                parts.append(skill_body)
            parts.append(new_code)

            return "\n\n".join(p for p in parts if p.strip())
        except Exception as exc:
            LOGGER.warning("[code_composer] Composition failed, using raw code: %s", exc)
            return new_code

    # ------------------------------------------------------------------
    # Import extraction
    # ------------------------------------------------------------------

    def _extract_imports(self, skills: list[Skill]) -> list[str]:
        """Return all import lines from every skill file."""
        lines: list[str] = []
        for skill in skills:
            code = self._read_skill_code(skill)
            if code:
                lines.extend(self._parse_import_lines(code))
        return lines

    @staticmethod
    def _parse_import_lines(code: str) -> list[str]:
        """Extract raw import/from-import lines from source code."""
        result: list[str] = []
        for line in code.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                result.append(stripped)
        return result

    def _deduplicate_imports(self, imports: list[str]) -> str:
        """Return unique import lines joined as a string, preserving order."""
        seen: set[str] = set()
        unique: list[str] = []
        for imp in imports:
            if imp not in seen:
                seen.add(imp)
                unique.append(imp)
        return "\n".join(unique)

    # ------------------------------------------------------------------
    # Function / class body extraction
    # ------------------------------------------------------------------

    def _merge_skill_functions(self, skills: list[Skill]) -> str:
        """
        Extract function and class definitions from skill files,
        skip duplicates (by name), and return them joined.
        """
        seen_names: set[str] = set()
        blocks: list[str] = []

        for skill in skills:
            code = self._read_skill_code(skill)
            if not code:
                continue
            for block in self._extract_definitions(code):
                name = self._def_name(block)
                if name and name not in seen_names:
                    seen_names.add(name)
                    blocks.append(block)
                elif not name:
                    blocks.append(block)

        return "\n\n".join(blocks)

    @staticmethod
    def _extract_definitions(code: str) -> list[str]:
        """
        Use AST to locate top-level function and class definitions.
        Returns the source lines for each definition.
        Falls back to returning non-import lines on parse failure.
        """
        lines = code.splitlines(keepends=True)
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Fallback: strip import lines and return the rest
            non_import = [
                l for l in lines
                if not l.strip().startswith(("import ", "from "))
            ]
            body = "".join(non_import).strip()
            return [body] if body else []

        defs: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = node.lineno - 1          # 0-indexed
                end = node.end_lineno or start + 1
                block = "".join(lines[start:end]).rstrip()
                defs.append(block)
        return defs

    @staticmethod
    def _def_name(block: str) -> str | None:
        """Extract the function/class name from the first line of a block."""
        first = block.strip().split("\n")[0].strip()
        for kw in ("async def ", "def ", "class "):
            if first.startswith(kw):
                rest = first[len(kw):]
                return rest.split("(")[0].split(":")[0].strip()
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_skill_code(skill: Skill) -> str:
        """Read skill source code from disk; return empty string on failure."""
        if not skill.code_path:
            return ""
        try:
            p = Path(skill.code_path)
            if p.exists():
                return p.read_text(encoding="utf-8")
            # Try relative to project root
            from pathlib import Path as _P
            alt = _P(__file__).parent.parent / skill.code_path
            if alt.exists():
                return alt.read_text(encoding="utf-8")
        except Exception as exc:
            LOGGER.warning("[code_composer] Cannot read skill %s: %s", skill.id, exc)
        return ""
