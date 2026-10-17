# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Template utilities for session memory rendering."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import jinja2
import yaml
from jinja2 import meta


def _to_yaml(value: Any) -> str:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    return yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False).rstrip()


def _template_environment(**kwargs: Any) -> jinja2.Environment:
    env = jinja2.Environment(autoescape=False, **kwargs)
    env.filters["to_yaml"] = _to_yaml
    return env


class TemplateUtils:
    @staticmethod
    def render(
        template: str,
        fields: Mapping[str, Any],
        extract_context: Any = None,
        *,
        debug_undefined: bool = False,
        keep_trailing_newline: bool = False,
        strip: bool = True,
    ) -> str:
        env = _template_environment(
            undefined=jinja2.DebugUndefined if debug_undefined else jinja2.Undefined,
            keep_trailing_newline=keep_trailing_newline,
        )
        template_vars = dict(fields)
        template_vars["extract_context"] = extract_context
        rendered = env.from_string(template).render(**template_vars)
        return rendered.strip() if strip else rendered

    @staticmethod
    def find_missing_variables(
        template: str,
        fields: Mapping[str, Any],
        *,
        ignored_variables: Iterable[str] | None = None,
    ) -> set[str]:
        parsed_template = _template_environment().parse(template)
        ignored = set(ignored_variables or ())
        return (
            meta.find_undeclared_variables(parsed_template)
            - set(fields)
            - ignored
            - {"extract_context"}
        )

    @staticmethod
    def referenced_variables(template: str) -> set[str]:
        """Return the top-level variables a template actually references.

        Uses Jinja's own parser so filters, functions, and attribute/method
        names (e.g. ``lower`` in ``{{ name|lower }}`` or ``get_year`` in
        ``{{ extract_context.get_year(ranges) }}``) are never mistaken for
        variables the way a plain substring/regex scan would.
        """
        parsed_template = _template_environment().parse(template)
        return meta.find_undeclared_variables(parsed_template)
