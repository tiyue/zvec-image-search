from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .tag_aliases import TagAliasDictionary

DEFAULT_MAX_EXPANSIONS_PER_FRAGMENT = 200
_TAG_QUERY_SEPARATOR = re.compile(r"[\s,，、;；|/\\]+")

TagMatchMode = Literal["all", "any"]


class TagSearchError(ValueError):
    """Base error for invalid or unsafe tag-search requests."""


class TagExpansionTooBroadError(TagSearchError):
    """Raised when one search fragment expands to too many full tags."""

    def __init__(self, fragment: str, match_count: int, limit: int) -> None:
        self.fragment = fragment
        self.match_count = match_count
        self.limit = limit
        super().__init__(
            f"Tag fragment {fragment!r} matched {match_count} tags, exceeding "
            f"the limit of {limit}. Refine the tag query."
        )


@dataclass(frozen=True)
class TagFragmentExpansion:
    """One user fragment and the exact stored tags that contain it."""

    fragment: str
    normalized_fragment: str
    matches: tuple[str, ...]
    expanded_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagSearchPlan:
    """Resolved tag-search input ready for a Collection query.

    ``matches_nothing`` must be checked before querying Zvec. A missing
    ``zvec_filter`` means either that no tag filter was requested or that the
    result is known to be empty; the boolean keeps those states unambiguous.
    """

    mode: TagMatchMode
    expansions: tuple[TagFragmentExpansion, ...]
    matched_tags: tuple[str, ...]
    zvec_filter: str | None
    matches_nothing: bool

    @property
    def has_filter(self) -> bool:
        return self.zvec_filter is not None


@dataclass(frozen=True)
class _CatalogTag:
    value: str
    normalized_value: str


class TagCatalog:
    """Immutable, per-Collection catalog used to expand partial tag queries.

    Matching uses NFKC plus ``casefold``. The exact stored spelling is retained
    in generated filters because Zvec's ARRAY_STRING filter is exact and may
    contain legacy case or width variants.
    """

    def __init__(
        self,
        tags: Iterable[str] | str | None = None,
        *,
        aliases: TagAliasDictionary | None = None,
    ) -> None:
        values = _coerce_values(tags)
        unique: dict[str, _CatalogTag] = {}
        for value in values:
            tag = _validate_catalog_tag(value)
            # Exact duplicates do not need duplicate filter literals. Width or
            # case variants remain separate because they can be distinct values
            # in an existing Zvec Collection.
            unique.setdefault(
                tag,
                _CatalogTag(
                    value=tag,
                    normalized_value=normalize_tag_search_text(tag),
                ),
            )
        self._tags = tuple(sorted(unique.values(), key=_catalog_sort_key))
        self._aliases = aliases or TagAliasDictionary()

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(tag.value for tag in self._tags)

    def resolve(
        self,
        fragments: Iterable[str] | str | None,
        *,
        mode: TagMatchMode = "all",
        max_expansions_per_fragment: int = DEFAULT_MAX_EXPANSIONS_PER_FRAGMENT,
    ) -> TagSearchPlan:
        """Expand fragments and build a safe exact-tag Zvec filter.

        In ``all`` mode every fragment must match at least one tag on a result.
        In ``any`` mode at least one fragment must match. A fragment matching
        more than ``max_expansions_per_fragment`` tags raises an explicit error
        rather than silently truncating the query and changing its meaning.
        """

        _validate_mode(mode)
        _validate_expansion_limit(max_expansions_per_fragment)
        normalized_fragments = _normalize_fragments(fragments)

        expansions: list[TagFragmentExpansion] = []
        for fragment, normalized_fragment in normalized_fragments:
            expanded_terms = self._aliases.equivalent_terms(fragment)
            normalized_equivalents = {
                normalize_tag_search_text(value) for value in expanded_terms
            }
            matches = tuple(
                tag.value
                for tag in self._tags
                if normalized_fragment in tag.normalized_value
                or tag.normalized_value in normalized_equivalents
            )
            if len(matches) > max_expansions_per_fragment:
                raise TagExpansionTooBroadError(
                    fragment,
                    len(matches),
                    max_expansions_per_fragment,
                )
            expansions.append(
                TagFragmentExpansion(
                    fragment=fragment,
                    normalized_fragment=normalized_fragment,
                    matches=matches,
                    expanded_terms=expanded_terms,
                )
            )

        return _build_plan(tuple(expansions), mode)


def resolve_tag_search(
    available_tags: Iterable[str] | str | None,
    fragments: Iterable[str] | str | None,
    *,
    mode: TagMatchMode = "all",
    aliases: TagAliasDictionary | None = None,
    max_expansions_per_fragment: int = DEFAULT_MAX_EXPANSIONS_PER_FRAGMENT,
) -> TagSearchPlan:
    """Convenience API for callers that do not need to reuse a catalog."""

    return TagCatalog(available_tags, aliases=aliases).resolve(
        fragments,
        mode=mode,
        max_expansions_per_fragment=max_expansions_per_fragment,
    )


def expand_fragments(
    available_tags: Iterable[str] | str | None,
    fragments: Iterable[str] | str | None,
    *,
    aliases: TagAliasDictionary | None = None,
    mode: TagMatchMode = "all",
    max_expansions_per_fragment: int = DEFAULT_MAX_EXPANSIONS_PER_FRAGMENT,
) -> TagSearchPlan:
    """Expand fuzzy tag fragments and configured aliases into an exact plan."""

    return resolve_tag_search(
        available_tags,
        fragments,
        mode=mode,
        aliases=aliases,
        max_expansions_per_fragment=max_expansions_per_fragment,
    )


def matched_tags_for_result(
    result_tags: Iterable[str] | str | None,
    plan: TagSearchPlan,
) -> tuple[str, ...]:
    """Return the exact result tags that satisfy any expansion in ``plan``.

    The function uses the already resolved catalog spellings from the plan, so
    callers get the same NFKC/case-insensitive behavior as the Zvec filter while
    retaining each result's original tag spelling for diagnostics and UI.
    """

    if not isinstance(plan, TagSearchPlan):
        raise TagSearchError("plan must be a TagSearchPlan.")
    if not plan.expansions:
        return ()
    allowed = {
        normalize_tag_search_text(tag)
        for expansion in plan.expansions
        for tag in expansion.matches
    }
    matched: dict[str, str] = {}
    for value in _coerce_values(result_tags):
        tag = _validate_catalog_tag(value)
        if normalize_tag_search_text(tag) in allowed:
            matched.setdefault(tag, tag)
    return tuple(sorted(matched.values(), key=_tag_value_sort_key))


def result_matches_tag_plan(
    result_tags: Iterable[str] | str | None,
    plan: TagSearchPlan,
) -> bool:
    """Return whether one document's tags satisfy a resolved fuzzy-tag plan.

    Zvec applies the generated exact-tag filter for semantic searches. Tag-only
    searches use the SQLite tag index instead, so they need the same ``all`` /
    ``any`` semantics without generating an embedding or querying a vector.
    """

    if not isinstance(plan, TagSearchPlan):
        raise TagSearchError("plan must be a TagSearchPlan.")
    if plan.matches_nothing:
        return False
    if not plan.expansions:
        return True

    normalized_result_tags = {
        normalize_tag_search_text(_validate_catalog_tag(value))
        for value in _coerce_values(result_tags)
    }
    expansion_matches = [
        any(
            normalize_tag_search_text(tag) in normalized_result_tags
            for tag in expansion.matches
        )
        for expansion in plan.expansions
    ]
    return all(expansion_matches) if plan.mode == "all" else any(expansion_matches)


def tag_match_score(expansion: TagFragmentExpansion, tag: str) -> float:
    """Return the deterministic relevance score for one resolved tag match.

    This score is deliberately a ranking signal, not a probability.  Keeping
    the calculation in the tag-search module lets both the Python fallback and
    the SQLite Top-N query use exactly the same semantics.
    """

    if not isinstance(expansion, TagFragmentExpansion):
        raise TagSearchError("expansion must be a TagFragmentExpansion.")
    normalized_tag = normalize_tag_search_text(_validate_catalog_tag(tag))
    allowed = {normalize_tag_search_text(value) for value in expansion.matches}
    if normalized_tag not in allowed:
        return 0.0
    equivalents = {
        normalize_tag_search_text(value) for value in expansion.expanded_terms
    }
    fragment = expansion.normalized_fragment
    if normalized_tag == fragment:
        return 1.0
    if normalized_tag in equivalents:
        return 0.96
    ratio = min(1.0, len(fragment) / max(1, len(normalized_tag)))
    if normalized_tag.startswith(fragment):
        return 0.85 + 0.10 * ratio
    if fragment in normalized_tag:
        return 0.70 + 0.15 * ratio
    return 0.65


def tag_match_confidence(
    plan: TagSearchPlan,
    matched_tags: Iterable[str] | str | None,
) -> float:
    """Score one result using the best match for each requested fragment."""

    if not isinstance(plan, TagSearchPlan):
        raise TagSearchError("plan must be a TagSearchPlan.")
    values: list[str] = []
    for value in _coerce_values(matched_tags):
        if not isinstance(value, str):
            raise TagSearchError("Tag search values must be strings.")
        values.append(value)
    if not values or not plan.expansions:
        return 0.0
    scores = [
        max((tag_match_score(expansion, tag) for tag in values), default=0.0)
        for expansion in plan.expansions
    ]
    return min(1.0, max(0.0, sum(scores) / len(scores)))


def normalize_tag_search_text(value: str) -> str:
    """Return the comparison form used by partial tag search."""

    if not isinstance(value, str):
        raise TagSearchError("Tag search values must be strings.")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise TagSearchError("Tag search values cannot be empty.")
    if _contains_control_character(normalized):
        raise TagSearchError("Tag search values cannot contain control characters.")
    return normalized.casefold()


def split_tag_search_query(value: str) -> tuple[str, ...]:
    """Split one tag-only query into stable, independently matched fragments."""

    if not isinstance(value, str):
        raise TagSearchError("Tag search query must be a string.")
    fragments = tuple(
        fragment
        for fragment in _TAG_QUERY_SEPARATOR.split(unicodedata.normalize("NFKC", value))
        if fragment
    )
    if not fragments:
        raise TagSearchError("Tag search query must contain at least one tag.")
    return tuple(display for display, _comparison in _normalize_fragments(fragments))


def quote_zvec_filter_string(value: str) -> str:
    """Quote an exact tag as a Zvec filter string literal."""

    if not isinstance(value, str):
        raise TagSearchError("Zvec filter values must be strings.")
    if not value:
        raise TagSearchError("Zvec filter values cannot be empty.")
    if _contains_control_character(value):
        raise TagSearchError("Zvec filter values cannot contain control characters.")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _build_plan(
    expansions: tuple[TagFragmentExpansion, ...], mode: TagMatchMode
) -> TagSearchPlan:
    matched_tags = _stable_tag_union(expansion.matches for expansion in expansions)
    if not expansions:
        return TagSearchPlan(
            mode=mode,
            expansions=(),
            matched_tags=(),
            zvec_filter=None,
            matches_nothing=False,
        )

    if mode == "all":
        matches_nothing = any(not expansion.matches for expansion in expansions)
        filter_groups = tuple(expansion.matches for expansion in expansions)
    else:
        matches_nothing = not matched_tags
        filter_groups = (matched_tags,)

    zvec_filter = (
        None if matches_nothing else _build_zvec_filter_from_groups(filter_groups, mode)
    )
    return TagSearchPlan(
        mode=mode,
        expansions=expansions,
        matched_tags=matched_tags,
        zvec_filter=zvec_filter,
        matches_nothing=matches_nothing,
    )


def _build_zvec_filter_from_groups(
    groups: tuple[tuple[str, ...], ...], mode: TagMatchMode
) -> str:
    clauses = []
    for group in groups:
        values = ",".join(quote_zvec_filter_string(tag) for tag in group)
        clauses.append(f"tags CONTAIN_ANY ({values})")
    if mode == "any":
        return clauses[0]
    return " AND ".join(f"({clause})" for clause in clauses)


def _normalize_fragments(
    fragments: Iterable[str] | str | None,
) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in _coerce_values(fragments):
        if not isinstance(value, str):
            raise TagSearchError("Tag search fragments must be strings.")
        display = unicodedata.normalize("NFKC", value).strip()
        comparison = normalize_tag_search_text(value)
        if comparison in seen:
            continue
        seen.add(comparison)
        normalized.append((display, comparison))
    return tuple(normalized)


def _validate_catalog_tag(value: object) -> str:
    if not isinstance(value, str):
        raise TagSearchError("Catalog tags must be strings.")
    tag = value.strip()
    if not tag:
        raise TagSearchError("Catalog tags cannot be empty.")
    if _contains_control_character(tag):
        raise TagSearchError("Catalog tags cannot contain control characters.")
    return tag


def _validate_mode(mode: object) -> None:
    if mode not in {"all", "any"}:
        raise TagSearchError("tag_mode must be 'all' or 'any'.")


def _validate_expansion_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise TagSearchError("max_expansions_per_fragment must be a positive integer.")


def _coerce_values(
    values: Iterable[str] | str | None,
) -> Iterable[object]:
    if values is None:
        return ()
    if isinstance(values, str):
        return (values,)
    try:
        return iter(values)
    except TypeError as exc:
        raise TagSearchError("Tag search values must be iterable.") from exc


def _stable_tag_union(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    unique = {tag for group in groups for tag in group}
    return tuple(sorted(unique, key=_tag_value_sort_key))


def _catalog_sort_key(tag: _CatalogTag) -> tuple[str, str, str]:
    return (tag.normalized_value, unicodedata.normalize("NFKC", tag.value), tag.value)


def _tag_value_sort_key(tag: str) -> tuple[str, str, str]:
    return (
        normalize_tag_search_text(tag),
        unicodedata.normalize("NFKC", tag),
        tag,
    )


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
