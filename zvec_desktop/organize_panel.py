"""Embeddable tkinter organize workbench for the pure-Python desktop client.

The panel owns presentation state only.  Every backend operation is injected as
an asynchronous protocol, every request is built by ``library_tasks`` or
``OrganizeReviewSession``, and worker completions are delivered through a queue
drained by Tk's main thread.
"""

from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Protocol

from .library_tasks import (
    AutoTagPendingRequest,
    AutoTagReviewRequest,
    AutoTagUndoRequest,
    IdentityConfirmationRequest,
    LowRiskBatchReviewRequest,
    TagAliasDeleteRequest,
    TagAliasListRequest,
    TagAliasUpsertRequest,
)
from .organize_state import (
    AliasCatalog,
    OrganizeDataError,
    OrganizeFilters,
    OrganizePage,
    OrganizeProposal,
    OrganizeReviewSession,
    OrganizeStateError,
    OrganizeValidationError,
    ReviewDecision,
    parse_alias_catalog,
    parse_alias_mutation_result,
    parse_pending_page,
    parse_review_result,
)
from .theme import DEFAULT_THEME, DesktopTheme
from .widgets import AsyncImageCanvas, ImageTaskDispatcher

AsyncResult = Future[object]
ConfirmCallback = Callable[[str, str], bool]
ErrorCallback = Callable[[str, str], None]
OpenImageCallback = Callable[[Path], None]
ThumbnailFactory = Callable[
    [tk.Misc, ImageTaskDispatcher, DesktopTheme], AsyncImageCanvas
]

_TAG_SPLIT = re.compile(r"[\s,，;；|｜]+")
_SUCCESS_JOB_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})
_FILTER_STATES = (
    ("全部", "all"),
    ("低风险", "low_risk"),
    ("身份待确认", "identity"),
    ("冲突", "conflict"),
    ("失败待补标", "failed"),
)
_STATE_LABELS = {
    "failed": "失败待补标",
    "conflict": "冲突",
    "identity": "身份待确认",
    "low_risk": "低风险",
    "pending": "待审核",
}


class OrganizeAsyncOperations(Protocol):
    """Asynchronous backend operations required by :class:`OrganizePanel`."""

    def load_pending(self, request: AutoTagPendingRequest) -> AsyncResult: ...

    def submit_review(self, request: AutoTagReviewRequest) -> AsyncResult: ...

    def submit_low_risk_batch(
        self, request: LowRiskBatchReviewRequest
    ) -> AsyncResult: ...

    def submit_identity_confirmation(
        self, request: IdentityConfirmationRequest
    ) -> AsyncResult: ...

    def undo_low_risk_batch(self, request: AutoTagUndoRequest) -> AsyncResult: ...

    def list_aliases(self, request: TagAliasListRequest) -> AsyncResult: ...

    def upsert_alias(self, request: TagAliasUpsertRequest) -> AsyncResult: ...

    def delete_alias(self, request: TagAliasDeleteRequest) -> AsyncResult: ...


@dataclass(frozen=True, slots=True)
class _Completion:
    operation: str
    token: int
    future: AsyncResult
    callback: Callable[[object], None]


@dataclass(frozen=True, slots=True)
class OrganizeResponsiveLayout:
    """Layout decisions derived from the available organize-page width."""

    filter_columns: int
    compact_actions: bool
    stacked_workspace: bool


def calculate_organize_layout(width: int) -> OrganizeResponsiveLayout:
    """Return a deterministic responsive layout for a logical-pixel width.

    Six filter cells fit comfortably in the normal desktop workspace.  A
    three-column grid produces the requested two-row layout in a narrow
    window, while a very small host gets two columns instead of clipped input
    fields.  Review and alias panes stack before either side becomes too thin
    to read safely.
    """

    if not isinstance(width, int) or isinstance(width, bool):
        raise TypeError("width must be an integer")
    available = max(1, width)
    filter_columns = 6 if available >= 980 else 3 if available >= 560 else 2
    return OrganizeResponsiveLayout(
        filter_columns=filter_columns,
        compact_actions=available < 1050,
        stacked_workspace=available < 900,
    )


def parse_tag_text(value: str) -> tuple[str, ...]:
    """Split the compact tag editor while preserving first-seen order."""

    if not isinstance(value, str):
        raise TypeError("value must be a string")
    result: list[str] = []
    seen: set[str] = set()
    for item in _TAG_SPLIT.split(value.strip()):
        tag = item.strip()
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return tuple(result)


class OrganizePanel(ttk.Frame):
    """Review up to one hundred suggestions per page without blocking Tk."""

    page_size = 100

    def __init__(
        self,
        master: tk.Misc,
        *,
        library_id: str,
        operations: OrganizeAsyncOperations,
        image_dispatcher: ImageTaskDispatcher,
        theme: DesktopTheme = DEFAULT_THEME,
        thumbnail_factory: ThumbnailFactory | None = None,
        on_open_image: OpenImageCallback | None = None,
        confirm: ConfirmCallback | None = None,
        report_error: ErrorCallback | None = None,
        auto_load: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        normalized_library = library_id.strip() if isinstance(library_id, str) else ""
        if not normalized_library:
            raise ValueError("library_id must be non-empty")
        self._library_id = normalized_library
        self._operations = operations
        self._image_dispatcher = image_dispatcher
        self._theme = theme
        self._thumbnail_factory = thumbnail_factory or _default_thumbnail_factory
        self._on_open_image = on_open_image
        self._confirm = confirm or self._default_confirm
        self._report_error = report_error or self._default_report_error
        self._ui_thread_id = threading.get_ident()
        self._last_ui_update_thread_id = self._ui_thread_id
        self._closed = False
        self._after_handle: str | None = None
        self._layout_after_handle: str | None = None
        self._completion_queue: queue.SimpleQueue[_Completion] = queue.SimpleQueue()
        self._operation_tokens: dict[str, int] = {}
        self._busy_operations: set[str] = set()
        self._page: OrganizePage | None = None
        self._session: OrganizeReviewSession | None = None
        self._aliases = parse_alias_catalog({"aliases": [], "count": 0})
        self._aliases_loaded = False
        self._row_to_proposal: dict[str, str] = {}
        self._proposal_to_row: dict[str, str] = {}
        self._selected_proposal_id: str | None = None
        self._identity_variables: dict[str, tk.BooleanVar] = {}
        self._synchronizing_editor = False
        self._responsive_layout: OrganizeResponsiveLayout | None = None
        self._filter_items: list[ttk.Frame] = []

        self._status_var = tk.StringVar(value="准备就绪")
        self._pending_var = tk.StringVar(value="待审核 0 张")
        self._page_var = tk.StringVar(value="第 0 / 0 页")
        self._progress_var = tk.StringVar(value="已审核 0 / 100")
        self._selection_var = tk.StringVar(value="低风险已选 0 张")
        self._issue_var = tk.StringVar(value="")
        self._review_empty_title_var = tk.StringVar(value="审核队列尚未加载")
        self._review_empty_hint_var = tk.StringVar(
            value="刷新后会在这里显示待审核的图片建议"
        )
        self._path_var = tk.StringVar(value="请选择待审核图片")
        self._description_var = tk.StringVar(value="")
        self._existing_var = tk.StringVar(value="现有标签：—")
        self._suggested_var = tk.StringVar(value="建议标签：—")
        self._difference_var = tk.StringVar(value="差异：—")
        self._decision_var = tk.StringVar(value="状态：待审核")
        self._edited_tags_var = tk.StringVar(value="")
        self._batch_var = tk.BooleanVar(value=False)

        self._latest_var = tk.BooleanVar(value=False)
        self._character_var = tk.StringVar(value="")
        self._work_var = tk.StringVar(value="")
        self._action_var = tk.StringVar(value="")
        self._expression_var = tk.StringVar(value="")
        self._review_state_var = tk.StringVar(value=_FILTER_STATES[0][0])

        self._alias_canonical_var = tk.StringVar(value="")
        self._alias_values_var = tk.StringVar(value="")
        self._alias_summary_var = tk.StringVar(value="别名词典尚未加载")
        self._alias_empty_title_var = tk.StringVar(value="别名词典尚未加载")
        self._alias_empty_hint_var = tk.StringVar(
            value="刷新后可维护角色、作品与人物的等价叫法"
        )

        self._configure_styles()
        self.configure(style="Organize.App.TFrame")
        self._build()
        self._bind_shortcuts()
        self._after_handle = self.after(25, self._drain_completions)
        if auto_load:
            self.after_idle(self.refresh)

    @property
    def library_id(self) -> str:
        return self._library_id

    @property
    def page(self) -> OrganizePage | None:
        return self._page

    @property
    def session(self) -> OrganizeReviewSession | None:
        return self._session

    @property
    def alias_catalog(self) -> AliasCatalog:
        return self._aliases

    @property
    def selected_proposal_id(self) -> str | None:
        return self._selected_proposal_id

    @property
    def last_ui_update_thread_id(self) -> int:
        return self._last_ui_update_thread_id

    @property
    def responsive_layout(self) -> OrganizeResponsiveLayout | None:
        return self._responsive_layout

    def set_library(self, library_id: str, *, refresh: bool = True) -> None:
        self._assert_ui_thread()
        normalized = library_id.strip() if isinstance(library_id, str) else ""
        if not normalized:
            raise ValueError("library_id must be non-empty")
        if normalized == self._library_id:
            return
        self._operation_tokens = {
            name: token + 1 for name, token in self._operation_tokens.items()
        }
        self._busy_operations.clear()
        self._library_id = normalized
        self._page = None
        self._session = None
        self._aliases = parse_alias_catalog({"aliases": [], "count": 0})
        self._aliases_loaded = False
        self._clear_review_widgets()
        self._clear_alias_widgets()
        if refresh:
            self.refresh(
                offset=0,
                prefer_previous=False,
                allow_discard=True,
            )

    def refresh(
        self,
        offset: int | None = None,
        *,
        prefer_previous: bool = True,
        allow_discard: bool = False,
    ) -> None:
        self._assert_ui_thread()
        if not allow_discard and not self._confirm_discard_changes():
            return
        requested_offset = (
            self._page.pagination.offset
            if offset is None and self._page is not None
            else 0
            if offset is None
            else max(0, offset)
        )
        requested_offset -= requested_offset % self.page_size
        try:
            filters = self._read_filters()
            request = AutoTagPendingRequest(
                library_id=self._library_id,
                offset=requested_offset,
                limit=self.page_size,
                filters=filters.to_backend_filters(),
            )
            future = self._operations.load_pending(request)
        except Exception as exc:
            self._show_error("无法加载待审核建议", exc)
            return
        if self._page is None:
            self._show_review_empty(
                "正在加载审核队列",
                "图片较多时可能需要几秒，窗口仍可正常操作",
            )
        self._set_status("正在加载待审核建议…")
        self._start_operation(
            "pending",
            future,
            lambda payload: self._apply_pending_payload(
                payload,
                prefer_previous=prefer_previous,
            ),
        )

    def apply_filters(self) -> None:
        self.refresh(offset=0, prefer_previous=False)

    def clear_filters(self) -> None:
        self._assert_ui_thread()
        if not self._confirm_discard_changes():
            return
        self._latest_var.set(False)
        self._character_var.set("")
        self._work_var.set("")
        self._action_var.set("")
        self._expression_var.set("")
        self._review_state_var.set(_FILTER_STATES[0][0])
        self.refresh(
            offset=0,
            prefer_previous=False,
            allow_discard=True,
        )

    def previous_page(self) -> None:
        self._assert_ui_thread()
        if self._page is None:
            return
        offset = self._page.pagination.previous_offset
        if offset is not None:
            self.refresh(offset=offset, prefer_previous=False)

    def next_page(self) -> None:
        self._assert_ui_thread()
        if self._page is None:
            return
        offset = self._page.pagination.next_offset
        if offset is not None:
            self.refresh(offset=offset, prefer_previous=True)

    def select_all_low_risk(self) -> None:
        self._assert_ui_thread()
        session = self._require_session()
        count = session.select_all_low_risk()
        self._refresh_all_rows()
        self._render_selected()
        self._set_status(f"已选择 {count} 张可安全批量接受的图片")

    def clear_low_risk_selection(self) -> None:
        self._assert_ui_thread()
        session = self._require_session()
        session.clear_low_risk_selection()
        self._refresh_all_rows()
        self._render_selected()

    def toggle_selected_batch(self) -> None:
        self._assert_ui_thread()
        proposal_id = self._require_selected_id()
        session = self._require_session()
        current = session.state_for(proposal_id).batch_selected
        try:
            session.select_low_risk(proposal_id, selected=not current)
        except OrganizeStateError as exc:
            self._show_error("无法选择低风险标签", exc)
            return
        self._update_row(proposal_id)
        self._render_selected()

    def mark_selected_accept(self) -> None:
        self._mark_selected("accept")

    def mark_selected_edit(self) -> None:
        proposal = self._selected_proposal()
        assert proposal is not None
        self._mark_selected("manual" if proposal.is_failed else "edit")

    def mark_selected_reject(self) -> None:
        self._mark_selected("reject")

    def confirm_selected_identity(self, tag: str, confirmed: bool) -> None:
        self._assert_ui_thread()
        proposal_id = self._require_selected_id()
        session = self._require_session()
        try:
            session.confirm_identity(proposal_id, tag, confirmed=confirmed)
        except OrganizeStateError as exc:
            self._show_error("无法确认身份标签", exc)
            return
        self._update_row(proposal_id)
        self._render_selected(keep_editor=True)

    def submit_selected_identity(self) -> None:
        self._assert_ui_thread()
        proposal_id = self._require_selected_id()
        session = self._require_session()
        self._commit_editor()
        try:
            request = session.build_identity_confirmation_request(
                self._library_id,
                proposal_id,
            )
        except OrganizeStateError as exc:
            self._show_error("身份标签尚未确认", exc)
            return
        try:
            future = self._operations.submit_identity_confirmation(request)
        except Exception as exc:
            self._show_error("无法提交身份确认", exc)
            return
        self._set_status("正在提交当前图片的身份确认…")
        self._start_operation(
            "identity_review",
            future,
            lambda payload: self._after_review(payload, "身份确认已写入"),
        )

    def submit_low_risk_batch(self) -> None:
        self._assert_ui_thread()
        session = self._require_session()
        try:
            request = session.build_low_risk_batch_request(self._library_id)
        except OrganizeStateError as exc:
            self._show_error("没有可批量接受的标签", exc)
            return
        if not self._confirm(
            "批量接受低风险标签",
            f"将为 {len(request.proposal_ids)} 张图片写入普通低风险标签。"
            "真人、Cosplayer、角色和作品身份不会批量写入。是否继续？",
        ):
            return
        try:
            future = self._operations.submit_low_risk_batch(request)
        except Exception as exc:
            self._show_error("无法提交批量审核", exc)
            return
        self._set_status("正在批量接受低风险标签…")
        self._start_operation("batch", future, self._after_batch)

    def submit_reviews(self) -> None:
        self._assert_ui_thread()
        session = self._require_session()
        self._commit_editor()
        try:
            request = session.build_review_request(self._library_id)
        except OrganizeStateError as exc:
            self._show_error("审核内容尚未完成", exc)
            return
        try:
            future = self._operations.submit_review(request)
        except Exception as exc:
            self._show_error("无法提交审核", exc)
            return
        self._set_status("正在写入审核结果…")
        self._start_operation(
            "review",
            future,
            lambda payload: self._after_review(payload, "审核结果已写入"),
        )

    def undo_last_batch(self) -> None:
        self._assert_ui_thread()
        session = self._require_session()
        if not session.undo_available:
            self._show_error(
                "没有可撤销操作",
                OrganizeValidationError(
                    "当前图库没有可撤销的最近一次批量操作。",
                    code="undo_unavailable",
                ),
            )
            return
        if not self._confirm(
            "撤销最近批量操作",
            "将恢复最近一次批量接受前的标签与待审核建议。是否继续？",
        ):
            return
        request = AutoTagUndoRequest(self._library_id)
        try:
            future = self._operations.undo_low_risk_batch(request)
        except Exception as exc:
            self._show_error("无法撤销批量操作", exc)
            return
        self._set_status("正在撤销最近一次批量操作…")
        self._start_operation("undo", future, self._after_undo)

    def refresh_aliases(self) -> None:
        self._assert_ui_thread()
        try:
            future = self._operations.list_aliases(
                TagAliasListRequest(self._library_id)
            )
        except Exception as exc:
            self._show_error("无法加载别名词典", exc)
            return
        if not self._aliases_loaded:
            self._show_alias_empty(
                "正在加载别名词典",
                "正在读取已维护的规范名称和别名关系",
            )
        self._set_status("正在加载别名词典…")
        self._start_operation("alias_list", future, self._apply_alias_payload)

    def new_alias(self) -> None:
        self._assert_ui_thread()
        self._alias_tree.selection_remove(self._alias_tree.selection())
        self._alias_canonical_var.set("")
        self._alias_values_var.set("")
        self._alias_canonical_entry.focus_set()

    def save_alias(self) -> None:
        self._assert_ui_thread()
        try:
            candidate = self._aliases.validate_upsert(
                self._alias_canonical_var.get(),
                parse_tag_text(self._alias_values_var.get()),
            )
            request = TagAliasUpsertRequest(
                library_id=self._library_id,
                canonical_name=candidate.canonical_name,
                aliases=candidate.aliases,
            )
            future = self._operations.upsert_alias(request)
        except Exception as exc:
            self._show_error("别名内容无效", exc)
            return
        self._set_status("正在保存别名关系…")
        self._start_operation("alias_mutation", future, self._after_alias_mutation)

    def delete_alias(self) -> None:
        self._assert_ui_thread()
        canonical = self._alias_canonical_var.get().strip()
        if not canonical:
            self._show_error(
                "请选择别名关系",
                OrganizeValidationError(
                    "请先选择需要删除的规范名称。",
                    field_name="canonical_name",
                ),
            )
            return
        if not self._confirm(
            "删除别名关系",
            f"确定删除“{canonical}”及其别名关系吗？已有图片标签不会被删除。",
        ):
            return
        try:
            request = TagAliasDeleteRequest(self._library_id, canonical)
            future = self._operations.delete_alias(request)
        except Exception as exc:
            self._show_error("无法删除别名关系", exc)
            return
        self._set_status("正在删除别名关系…")
        self._start_operation("alias_mutation", future, self._after_alias_mutation)

    def close(self) -> None:
        self._assert_ui_thread()
        if self._closed:
            return
        self._closed = True
        self._operation_tokens = {
            name: token + 1 for name, token in self._operation_tokens.items()
        }
        if self._after_handle is not None:
            with suppress(tk.TclError):
                self.after_cancel(self._after_handle)
            self._after_handle = None
        if self._layout_after_handle is not None:
            with suppress(tk.TclError):
                self.after_cancel(self._layout_after_handle)
            self._layout_after_handle = None
        while True:
            try:
                self._completion_queue.get_nowait()
            except queue.Empty:
                break

    def destroy(self) -> None:
        self.close()
        super().destroy()

    def _configure_styles(self) -> None:
        """Register page-local semantic styles for embedded and standalone use."""

        style = ttk.Style(self)
        theme = self._theme
        font = theme.typography.family
        style.configure("Organize.App.TFrame", background=theme.window)
        style.configure("Organize.Surface.TFrame", background=theme.surface)
        style.configure(
            "Organize.Panel.TFrame",
            background=theme.surface,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Flow.TFrame",
            background=theme.primary_soft,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Command.TFrame",
            background=theme.surface_subtle,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Editor.TFrame",
            background=theme.surface_subtle,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Identity.TFrame",
            background=theme.warning_soft,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Empty.TFrame",
            background=theme.surface_subtle,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Organize.Card.TFrame",
            background=theme.surface,
            bordercolor=theme.border,
            lightcolor=theme.border,
            darkcolor=theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Organize.Subtle.TFrame",
            background=theme.surface_subtle,
        )
        style.configure(
            "Organize.StatusBar.TFrame",
            background=theme.surface,
            bordercolor=theme.border,
            borderwidth=1,
            relief="flat",
        )
        style.configure(
            "Organize.Section.TLabel",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.section, "bold"),
        )
        style.configure(
            "Organize.Flow.TLabel",
            background=theme.primary_soft,
            foreground=theme.primary_pressed,
            font=(font, theme.typography.control, "bold"),
        )
        style.configure(
            "Organize.FlowHint.TLabel",
            background=theme.primary_soft,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "Organize.CommandSection.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text,
            font=(font, theme.typography.section, "bold"),
        )
        style.configure(
            "Organize.CommandHint.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_faint,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "Organize.EditorSection.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text,
            font=(font, theme.typography.control, "bold"),
        )
        style.configure(
            "Organize.EditorMuted.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.EditorAccent.TLabel",
            background=theme.surface_subtle,
            foreground=theme.primary_pressed,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "Organize.IdentityWarning.TLabel",
            background=theme.warning_soft,
            foreground=theme.warning,
            font=(font, theme.typography.supporting, "bold"),
        )
        style.configure(
            "Organize.EmptyTitle.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text,
            font=(font, theme.typography.section, "bold"),
        )
        style.configure(
            "Organize.EmptyHint.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_faint,
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.Muted.TLabel",
            background=theme.surface,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.Hint.TLabel",
            background=theme.surface,
            foreground=theme.text_faint,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "Organize.Subtle.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.Accent.TLabel",
            background=theme.surface,
            foreground=theme.primary_pressed,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "Organize.Warning.TLabel",
            background=theme.surface,
            foreground=theme.warning,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "Organize.Metric.TLabel",
            background=theme.surface_muted,
            foreground=theme.text_muted,
            padding=(8, 4),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.configure(
            "Organize.PendingMetric.TLabel",
            background=theme.warning_soft,
            foreground=theme.warning,
            padding=(8, 4),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.configure(
            "Organize.ProgressMetric.TLabel",
            background=theme.success_soft,
            foreground=theme.success,
            padding=(8, 4),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.configure(
            "Organize.SelectionMetric.TLabel",
            background=theme.primary_soft,
            foreground=theme.primary_pressed,
            padding=(8, 4),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.configure(
            "Organize.DiffExisting.TLabel",
            background=theme.surface_muted,
            foreground=theme.text_muted,
            padding=(9, 6),
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.DiffSuggested.TLabel",
            background=theme.primary_soft,
            foreground=theme.primary_pressed,
            padding=(9, 6),
            font=(font, theme.typography.body),
        )
        style.configure(
            "Organize.DiffChange.TLabel",
            background=theme.success_soft,
            foreground=theme.success,
            padding=(9, 6),
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "Organize.Status.TLabel",
            background=theme.surface,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )

        for name, background, foreground, border, padding, weight in (
            (
                "Organize.Primary.TButton",
                theme.primary,
                "#FFFFFF",
                theme.primary,
                (15, 8),
                "bold",
            ),
            (
                "Organize.Secondary.TButton",
                theme.primary_soft,
                theme.primary_pressed,
                theme.primary_soft,
                (12, 7),
                "bold",
            ),
            (
                "Organize.Quiet.TButton",
                theme.surface,
                theme.text,
                theme.border_strong,
                (11, 7),
                "normal",
            ),
            (
                "Organize.Danger.TButton",
                theme.surface,
                theme.danger,
                theme.danger_soft,
                (11, 7),
                "normal",
            ),
        ):
            style.configure(
                name,
                background=background,
                foreground=foreground,
                bordercolor=border,
                lightcolor=border,
                darkcolor=border,
                focuscolor=border,
                borderwidth=1,
                padding=padding,
                font=(font, theme.typography.control, weight),
            )
        style.map(
            "Organize.Primary.TButton",
            background=[
                ("pressed", theme.primary_pressed),
                ("active", theme.primary_hover),
                ("disabled", theme.surface_strong),
            ],
            bordercolor=[
                ("pressed", theme.primary_pressed),
                ("active", theme.primary_hover),
                ("disabled", theme.surface_strong),
            ],
            foreground=[("disabled", theme.text_faint)],
        )
        style.map(
            "Organize.Secondary.TButton",
            background=[
                ("active", theme.selection),
                ("disabled", theme.surface_subtle),
            ],
            foreground=[("disabled", theme.text_faint)],
        )
        style.map(
            "Organize.Quiet.TButton",
            background=[
                ("active", theme.surface_muted),
                ("disabled", theme.surface_subtle),
            ],
            foreground=[("disabled", theme.text_faint)],
        )
        style.map(
            "Organize.Danger.TButton",
            background=[
                ("active", theme.danger_soft),
                ("disabled", theme.surface_subtle),
            ],
            foreground=[("disabled", theme.text_faint)],
        )

        style.configure(
            "Organize.TEntry",
            fieldbackground=theme.surface,
            foreground=theme.text,
            bordercolor=theme.border_strong,
            lightcolor=theme.border_strong,
            darkcolor=theme.border_strong,
            insertcolor=theme.primary,
            padding=(8, 6),
            borderwidth=1,
        )
        style.map(
            "Organize.TEntry",
            bordercolor=[("focus", theme.primary)],
            lightcolor=[("focus", theme.primary)],
            darkcolor=[("focus", theme.primary)],
        )
        style.configure(
            "Organize.TCombobox",
            fieldbackground=theme.surface,
            background=theme.surface,
            foreground=theme.text,
            arrowcolor=theme.text_muted,
            bordercolor=theme.border_strong,
            lightcolor=theme.border_strong,
            darkcolor=theme.border_strong,
            padding=(7, 6),
        )
        style.map(
            "Organize.TCombobox",
            fieldbackground=[("readonly", theme.surface)],
            bordercolor=[("focus", theme.primary)],
            lightcolor=[("focus", theme.primary)],
            darkcolor=[("focus", theme.primary)],
        )
        style.configure(
            "Organize.TCheckbutton",
            background=theme.surface,
            foreground=theme.text_muted,
            indicatorbackground=theme.surface,
            indicatorforeground=theme.primary,
            padding=(2, 2),
        )
        style.map(
            "Organize.TCheckbutton",
            background=[("active", theme.surface)],
            foreground=[("active", theme.text)],
            indicatorbackground=[("selected", theme.primary)],
        )
        style.configure(
            "Organize.Editor.TCheckbutton",
            background=theme.surface_subtle,
            foreground=theme.text_muted,
            indicatorbackground=theme.surface,
            indicatorforeground=theme.primary,
            padding=(2, 2),
        )
        style.map(
            "Organize.Editor.TCheckbutton",
            background=[("active", theme.surface_subtle)],
            foreground=[("active", theme.text)],
            indicatorbackground=[("selected", theme.primary)],
        )
        style.configure(
            "Organize.Identity.TCheckbutton",
            background=theme.warning_soft,
            foreground=theme.text,
            indicatorbackground=theme.surface,
            indicatorforeground=theme.primary,
            padding=(2, 2),
        )
        style.map(
            "Organize.Identity.TCheckbutton",
            background=[("active", theme.warning_soft)],
            foreground=[("active", theme.text)],
            indicatorbackground=[("selected", theme.primary)],
        )
        style.configure(
            "Organize.Treeview",
            background=theme.surface,
            fieldbackground=theme.surface,
            foreground=theme.text,
            bordercolor=theme.border,
            rowheight=30,
            font=(font, theme.typography.body),
        )
        style.map(
            "Organize.Treeview",
            background=[("selected", theme.selection)],
            foreground=[("selected", theme.text)],
        )
        style.configure(
            "Organize.Treeview.Heading",
            background=theme.surface_muted,
            foreground=theme.text_muted,
            bordercolor=theme.border,
            padding=(8, 7),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.map(
            "Organize.Treeview.Heading",
            background=[("active", theme.surface_strong)],
        )
        style.configure(
            "Organize.Card.TLabelframe",
            background=theme.surface,
            bordercolor=theme.border,
            lightcolor=theme.border,
            darkcolor=theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Organize.Card.TLabelframe.Label",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.control, "bold"),
        )
        style.configure(
            "Organize.TNotebook",
            background=theme.window,
            bordercolor=theme.border,
            borderwidth=0,
            tabmargins=(0, 0, 0, 8),
        )
        style.configure(
            "Organize.TNotebook.Tab",
            background=theme.surface_muted,
            foreground=theme.text_muted,
            borderwidth=0,
            padding=(15, 8),
            font=(font, theme.typography.control),
        )
        style.map(
            "Organize.TNotebook.Tab",
            background=[
                ("selected", theme.primary_soft),
                ("active", theme.surface_strong),
            ],
            foreground=[("selected", theme.primary_pressed)],
        )
        style.configure(
            "Organize.Vertical.TScrollbar",
            background=theme.border_strong,
            troughcolor=theme.surface_subtle,
            bordercolor=theme.surface_subtle,
            arrowcolor=theme.text_muted,
            width=12,
        )

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        spacing = self._theme.spacing
        self._notebook = ttk.Notebook(self, style="Organize.TNotebook")
        self._notebook.grid(row=0, column=0, sticky="nsew")
        self._review_tab = ttk.Frame(
            self._notebook,
            style="Organize.App.TFrame",
            padding=spacing.sm,
        )
        self._alias_tab = ttk.Frame(
            self._notebook,
            style="Organize.App.TFrame",
            padding=spacing.sm,
        )
        self._notebook.add(self._review_tab, text="审核建议")
        self._notebook.add(self._alias_tab, text="别名词典")
        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add=True)
        self._build_review_tab()
        self._build_alias_tab()
        self.bind("<Configure>", self._on_panel_configure, add=True)

        status = ttk.Frame(
            self,
            style="Organize.StatusBar.TFrame",
            padding=(spacing.md, spacing.sm),
        )
        status.grid(row=1, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(
            status,
            textvariable=self._status_var,
            style="Organize.Status.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._shortcut_label = ttk.Label(
            status,
            text="快捷键：A 接受 · E 编辑 · R 拒绝 · Space 批量勾选 · "
            "Ctrl+Enter 提交 · [ / ] 翻页",
            style="Organize.Hint.TLabel",
        )
        self._shortcut_label.grid(row=0, column=1, sticky="e")
        self._apply_responsive_layout(1200)
        self._update_controls()

    def _build_review_tab(self) -> None:
        spacing = self._theme.spacing
        host = self._review_tab
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self._review_canvas = tk.Canvas(
            host,
            background=self._theme.window,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self._review_canvas.grid(row=0, column=0, sticky="nsew")
        self._review_scrollbar = ttk.Scrollbar(
            host,
            orient="vertical",
            command=self._review_canvas.yview,
            style="Organize.Vertical.TScrollbar",
        )
        self._review_scrollbar.grid(row=0, column=1, sticky="ns")
        self._review_canvas.configure(yscrollcommand=self._review_scrollbar.set)
        tab = ttk.Frame(
            self._review_canvas,
            style="Organize.App.TFrame",
            padding=(0, 0, spacing.xs, 0),
        )
        self._review_content = tab
        self._review_canvas_window = self._review_canvas.create_window(
            (0, 0),
            anchor="nw",
            window=tab,
        )
        self._review_canvas.bind(
            "<Configure>",
            self._on_review_canvas_configure,
            add=True,
        )
        tab.bind("<Configure>", self._on_review_content_configure, add=True)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        flow = ttk.Frame(
            tab,
            style="Organize.Flow.TFrame",
            padding=(spacing.md, spacing.sm),
        )
        flow.grid(row=0, column=0, sticky="ew", pady=(0, spacing.sm))
        flow.columnconfigure(1, weight=1)
        self._flow_steps = ttk.Label(
            flow,
            text=(
                "1  筛选队列   →   2  审核图片   →   3  对比差异   →   "
                "4  批量或逐项提交"
            ),
            style="Organize.Flow.TLabel",
        )
        self._flow_steps.grid(row=0, column=0, sticky="w")
        self._flow_hint = ttk.Label(
            flow,
            text="身份类标签不会自动批量写入",
            style="Organize.FlowHint.TLabel",
        )
        self._flow_hint.grid(row=0, column=1, sticky="e", padx=(spacing.md, 0))

        filters = ttk.Frame(
            tab,
            style="Organize.Panel.TFrame",
            padding=(spacing.md, spacing.sm),
        )
        filters.grid(row=1, column=0, sticky="ew", pady=(0, spacing.sm))
        filters.columnconfigure(0, weight=1)
        filters.columnconfigure(1, weight=0)
        filter_heading = ttk.Frame(filters, style="Organize.Surface.TFrame")
        filter_heading.grid(row=0, column=0, sticky="ew", pady=(0, spacing.sm))
        filter_heading.columnconfigure(1, weight=1)
        ttk.Label(
            filter_heading,
            text="筛选建议",
            style="Organize.Section.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._filter_hint = ttk.Label(
            filter_heading,
            text="按来源范围、内容和审核状态快速缩小队列",
            style="Organize.Hint.TLabel",
        )
        self._filter_hint.grid(row=0, column=1, sticky="w", padx=(spacing.md, 0))

        self._filter_grid = ttk.Frame(filters, style="Organize.Surface.TFrame")
        self._filter_grid.grid(row=1, column=0, columnspan=2, sticky="ew")
        latest = ttk.Frame(self._filter_grid, style="Organize.Surface.TFrame")
        ttk.Label(latest, text="范围", style="Organize.Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, spacing.xs)
        )
        ttk.Checkbutton(
            latest,
            text="仅本次新增图片",
            variable=self._latest_var,
            style="Organize.TCheckbutton",
        ).grid(row=1, column=0, sticky="w")
        character = self._filter_entry(
            self._filter_grid,
            "角色",
            self._character_var,
        )
        work = self._filter_entry(self._filter_grid, "作品", self._work_var)
        action = self._filter_entry(self._filter_grid, "动作", self._action_var)
        expression = self._filter_entry(
            self._filter_grid,
            "神态",
            self._expression_var,
        )
        state = ttk.Frame(self._filter_grid, style="Organize.Surface.TFrame")
        state.columnconfigure(0, weight=1)
        ttk.Label(state, text="审核状态", style="Organize.Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, spacing.xs)
        )
        self._review_state_combo = ttk.Combobox(
            state,
            textvariable=self._review_state_var,
            values=[label for label, _value in _FILTER_STATES],
            state="readonly",
            style="Organize.TCombobox",
        )
        self._review_state_combo.grid(row=1, column=0, sticky="ew")
        self._filter_items = [latest, character, work, action, expression, state]

        filter_actions = ttk.Frame(filters, style="Organize.Surface.TFrame")
        filter_actions.grid(
            row=0,
            column=1,
            sticky="e",
            padx=(spacing.md, 0),
            pady=(0, spacing.sm),
        )
        ttk.Button(
            filter_actions,
            text="清除筛选",
            command=self.clear_filters,
            style="Organize.Quiet.TButton",
        ).pack(side="left")
        ttk.Button(
            filter_actions,
            text="应用筛选",
            command=self.apply_filters,
            style="Organize.Secondary.TButton",
        ).pack(side="left", padx=(spacing.sm, 0))

        actions = ttk.Frame(
            host,
            style="Organize.Command.TFrame",
            padding=(spacing.md, spacing.sm),
        )
        actions.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(spacing.sm, 0),
        )
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self._review_toolbar = actions
        self._toolbar_heading = ttk.Frame(actions, style="Organize.Command.TFrame")
        self._toolbar_heading.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._toolbar_heading.columnconfigure(1, weight=1)
        ttk.Label(
            self._toolbar_heading,
            text="审核进度与提交",
            style="Organize.CommandSection.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._toolbar_hint = ttk.Label(
            self._toolbar_heading,
            text="先确认当前页，再执行批量或逐项提交；所有未提交修改都可继续调整",
            style="Organize.CommandHint.TLabel",
        )
        self._toolbar_hint.grid(
            row=0,
            column=1,
            sticky="w",
            padx=(spacing.md, 0),
        )

        self._navigation_actions = ttk.Frame(
            actions,
            style="Organize.Command.TFrame",
        )
        self._refresh_button = ttk.Button(
            self._navigation_actions,
            text="刷新",
            command=self.refresh,
            style="Organize.Quiet.TButton",
        )
        self._refresh_button.pack(side="left")
        self._previous_button = ttk.Button(
            self._navigation_actions,
            text="上一页 [",
            command=self.previous_page,
            style="Organize.Quiet.TButton",
        )
        self._previous_button.pack(side="left", padx=(spacing.sm, 0))
        self._next_button = ttk.Button(
            self._navigation_actions,
            text="下一页 ]",
            command=self.next_page,
            style="Organize.Quiet.TButton",
        )
        self._next_button.pack(side="left", padx=(spacing.sm, 0))

        self._review_metrics = ttk.Frame(actions, style="Organize.Command.TFrame")
        for variable, metric_style in (
            (self._pending_var, "Organize.PendingMetric.TLabel"),
            (self._progress_var, "Organize.ProgressMetric.TLabel"),
            (self._selection_var, "Organize.SelectionMetric.TLabel"),
            (self._page_var, "Organize.Metric.TLabel"),
        ):
            ttk.Label(
                self._review_metrics,
                textvariable=variable,
                style=metric_style,
            ).pack(side="left", padx=(spacing.sm, 0))

        self._selection_actions = ttk.Frame(
            actions,
            style="Organize.Command.TFrame",
        )
        self._select_all_button = ttk.Button(
            self._selection_actions,
            text="全选低风险",
            command=self.select_all_low_risk,
            style="Organize.Quiet.TButton",
        )
        self._select_all_button.pack(side="left")
        self._clear_selection_button = ttk.Button(
            self._selection_actions,
            text="清除选择",
            command=self.clear_low_risk_selection,
            style="Organize.Quiet.TButton",
        )
        self._clear_selection_button.pack(side="left", padx=(spacing.sm, 0))
        self._batch_button = ttk.Button(
            self._selection_actions,
            text="批量接受低风险",
            command=self.submit_low_risk_batch,
            style="Organize.Secondary.TButton",
        )
        self._batch_button.pack(side="left", padx=(spacing.sm, 0))

        self._commit_actions = ttk.Frame(actions, style="Organize.Command.TFrame")
        self._submit_button = ttk.Button(
            self._commit_actions,
            text="提交逐项审核",
            command=self.submit_reviews,
            style="Organize.Primary.TButton",
        )
        self._submit_button.pack(side="left")
        self._undo_button = ttk.Button(
            self._commit_actions,
            text="撤销最近批量",
            command=self.undo_last_batch,
            style="Organize.Danger.TButton",
        )
        self._undo_button.pack(side="left", padx=(spacing.sm, 0))

        self._review_workspace = ttk.Frame(tab, style="Organize.App.TFrame")
        self._review_workspace.grid(row=2, column=0, sticky="nsew")
        self._review_list_card = ttk.Frame(
            self._review_workspace,
            style="Organize.Card.TFrame",
            padding=spacing.md,
        )
        self._review_detail_card = ttk.Frame(
            self._review_workspace,
            style="Organize.Card.TFrame",
            padding=spacing.md,
        )
        self._build_review_list(self._review_list_card)
        self._build_review_detail(self._review_detail_card)

        issue = ttk.Label(
            tab,
            textvariable=self._issue_var,
            style="Organize.Warning.TLabel",
        )
        issue.grid(row=3, column=0, sticky="w", pady=(spacing.xs, 0))

    def _filter_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
    ) -> ttk.Frame:
        field = ttk.Frame(parent, style="Organize.Surface.TFrame")
        field.columnconfigure(0, weight=1)
        ttk.Label(field, text=label, style="Organize.Muted.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, self._theme.spacing.xs),
        )
        entry = ttk.Entry(
            field,
            textvariable=variable,
            style="Organize.TEntry",
        )
        entry.grid(row=1, column=0, sticky="ew")
        entry.bind("<Return>", lambda _event: self.apply_filters(), add=True)
        return field

    def _build_review_list(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        heading = ttk.Frame(parent, style="Organize.Surface.TFrame")
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        heading.columnconfigure(1, weight=1)
        ttk.Label(
            heading,
            text="待审核图片",
            style="Organize.Section.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._review_list_hint = ttk.Label(
            heading,
            text="单页最多 100 张 · 选择一行查看来源与差异",
            style="Organize.Hint.TLabel",
        )
        self._review_list_hint.grid(row=0, column=1, sticky="e")
        columns = ("batch", "state", "path", "low", "identity", "difference")
        self._review_tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Organize.Treeview",
        )
        self._review_tree.tag_configure("accepted", foreground=self._theme.success)
        self._review_tree.tag_configure(
            "edited",
            foreground=self._theme.primary_pressed,
        )
        self._review_tree.tag_configure("rejected", foreground=self._theme.danger)
        self._review_tree.tag_configure("identity", foreground=self._theme.warning)
        self._review_tree.tag_configure("failed", foreground=self._theme.danger)
        self._review_tree.tag_configure("low_risk", foreground=self._theme.success)
        headings = {
            "batch": "批量",
            "state": "状态",
            "path": "图片",
            "low": "低风险新增",
            "identity": "身份待确认",
            "difference": "现有 / 建议差异",
        }
        widths = {
            "batch": 46,
            "state": 82,
            "path": 190,
            "low": 125,
            "identity": 125,
            "difference": 130,
        }
        for name in columns:
            self._review_tree.heading(name, text=headings[name])
            self._review_tree.column(
                name,
                width=widths[name],
                minwidth=40,
                stretch=name in {"path", "difference"},
            )
        self._review_tree_scrollbar = ttk.Scrollbar(
            parent,
            orient="vertical",
            command=self._review_tree.yview,
            style="Organize.Vertical.TScrollbar",
        )
        self._review_tree.configure(yscrollcommand=self._review_tree_scrollbar.set)
        self._review_tree.grid(row=1, column=0, sticky="nsew")
        self._review_tree_scrollbar.grid(row=1, column=1, sticky="ns")
        self._review_tree.bind("<<TreeviewSelect>>", self._on_review_select, add=True)

        self._review_empty = ttk.Frame(
            parent,
            style="Organize.Empty.TFrame",
            padding=(self._theme.spacing.xl, self._theme.spacing.xl),
        )
        self._review_empty.columnconfigure(0, weight=1)
        self._review_empty.rowconfigure(0, weight=1)
        empty_content = ttk.Frame(
            self._review_empty,
            style="Organize.Empty.TFrame",
        )
        empty_content.grid(row=0, column=0)
        ttk.Label(
            empty_content,
            textvariable=self._review_empty_title_var,
            style="Organize.EmptyTitle.TLabel",
        ).pack()
        ttk.Label(
            empty_content,
            textvariable=self._review_empty_hint_var,
            style="Organize.EmptyHint.TLabel",
            justify="center",
            wraplength=420,
        ).pack(pady=(self._theme.spacing.xs, 0))
        self._review_empty.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self._show_review_empty(
            "审核队列尚未加载",
            "刷新后会在这里显示待审核的图片建议",
        )

    def _build_review_detail(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(6, weight=1)
        detail_heading = ttk.Frame(parent, style="Organize.Surface.TFrame")
        detail_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        detail_heading.columnconfigure(1, weight=1)
        ttk.Label(
            detail_heading,
            text="图片与标签差异",
            style="Organize.Section.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._detail_hint = ttk.Label(
            detail_heading,
            text="双击预览可打开原图",
            style="Organize.Hint.TLabel",
        )
        self._detail_hint.grid(row=0, column=1, sticky="e")
        self._preview = self._thumbnail_factory(
            parent,
            self._image_dispatcher,
            self._theme,
        )
        self._preview.grid(row=1, column=0, sticky="ew")
        self._preview.configure(height=190)
        self._preview.bind("<Double-Button-1>", self._open_selected_image, add=True)
        ttk.Label(
            parent,
            textvariable=self._path_var,
            style="Organize.Section.TLabel",
            wraplength=480,
        ).grid(row=2, column=0, sticky="w", pady=(8, 2))
        ttk.Label(
            parent,
            textvariable=self._description_var,
            style="Organize.Muted.TLabel",
            wraplength=480,
        ).grid(row=3, column=0, sticky="w")

        differences = ttk.Frame(
            parent,
            style="Organize.Surface.TFrame",
        )
        differences.grid(row=4, column=0, sticky="ew", pady=(8, 6))
        differences.columnconfigure(0, weight=1)
        for row, variable, difference_style in (
            (0, self._existing_var, "Organize.DiffExisting.TLabel"),
            (1, self._suggested_var, "Organize.DiffSuggested.TLabel"),
            (2, self._difference_var, "Organize.DiffChange.TLabel"),
        ):
            ttk.Label(
                differences,
                textvariable=variable,
                style=difference_style,
                wraplength=480,
            ).grid(
                row=row,
                column=0,
                sticky="ew",
                pady=(0, self._theme.spacing.xs if row < 2 else 0),
            )

        ttk.Label(
            parent,
            text="标签来源",
            style="Organize.Muted.TLabel",
        ).grid(row=5, column=0, sticky="w")
        source_frame = ttk.Frame(parent, style="Organize.Surface.TFrame")
        source_frame.grid(row=6, column=0, sticky="nsew", pady=(4, 8))
        source_frame.columnconfigure(0, weight=1)
        source_frame.rowconfigure(0, weight=1)
        self._source_tree = ttk.Treeview(
            source_frame,
            columns=("tag", "source", "state", "confidence"),
            show="headings",
            height=6,
            style="Organize.Treeview",
        )
        for name, label, width in (
            ("tag", "标签", 110),
            ("source", "来源", 120),
            ("state", "状态", 110),
            ("confidence", "置信", 65),
        ):
            self._source_tree.heading(name, text=label)
            self._source_tree.column(name, width=width, stretch=name == "tag")
        source_scroll = ttk.Scrollbar(
            source_frame,
            orient="vertical",
            command=self._source_tree.yview,
            style="Organize.Vertical.TScrollbar",
        )
        self._source_tree.configure(yscrollcommand=source_scroll.set)
        self._source_tree.grid(row=0, column=0, sticky="nsew")
        source_scroll.grid(row=0, column=1, sticky="ns")

        editor = ttk.Frame(
            parent,
            style="Organize.Editor.TFrame",
            padding=(self._theme.spacing.md, self._theme.spacing.sm),
        )
        editor.grid(row=7, column=0, sticky="ew")
        editor.columnconfigure(1, weight=1)
        ttk.Label(
            editor,
            text="逐项审核",
            style="Organize.EditorSection.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            editor,
            text="编辑普通标签；身份类标签需在下方逐项确认",
            style="Organize.CommandHint.TLabel",
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(
            editor,
            text="写入标签",
            style="Organize.EditorMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(self._theme.spacing.sm, 0))
        self._edited_tags_entry = ttk.Entry(
            editor,
            textvariable=self._edited_tags_var,
            style="Organize.TEntry",
        )
        self._edited_tags_entry.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(self._theme.spacing.sm, 0),
            pady=(self._theme.spacing.sm, 0),
        )
        self._edited_tags_entry.bind("<Return>", self._on_editor_return, add=True)
        self._edited_tags_entry.bind("<FocusOut>", self._on_editor_focus_out, add=True)

        self._identity_frame = ttk.Frame(
            editor,
            style="Organize.Editor.TFrame",
            padding=(self._theme.spacing.sm, self._theme.spacing.xs),
        )
        self._identity_frame.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(self._theme.spacing.sm, self._theme.spacing.xs),
        )
        self._batch_check = ttk.Checkbutton(
            editor,
            text="加入低风险批量",
            variable=self._batch_var,
            command=self._on_batch_toggle,
            style="Organize.Editor.TCheckbutton",
        )
        self._batch_check.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 4),
        )
        self._decision_buttons = ttk.Frame(editor, style="Organize.Editor.TFrame")
        self._decision_buttons.grid(row=4, column=0, columnspan=2, sticky="ew")
        for column in range(3):
            self._decision_buttons.columnconfigure(column, weight=1)
        self._accept_button = ttk.Button(
            self._decision_buttons,
            text="接受 A",
            command=self.mark_selected_accept,
            style="Organize.Secondary.TButton",
        )
        self._accept_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self._edit_button = ttk.Button(
            self._decision_buttons,
            text="编辑 E",
            command=self.mark_selected_edit,
            style="Organize.Quiet.TButton",
        )
        self._edit_button.grid(row=0, column=1, sticky="ew", padx=3)
        self._reject_button = ttk.Button(
            self._decision_buttons,
            text="拒绝 R",
            command=self.mark_selected_reject,
            style="Organize.Danger.TButton",
        )
        self._reject_button.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        self._identity_submit_button = ttk.Button(
            self._decision_buttons,
            text="仅提交当前身份",
            command=self.submit_selected_identity,
            style="Organize.Secondary.TButton",
        )
        self._identity_submit_button.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(self._theme.spacing.sm, 0),
        )
        self._decision_label = ttk.Label(
            self._decision_buttons,
            textvariable=self._decision_var,
            style="Organize.EditorAccent.TLabel",
        )
        self._decision_label.grid(
            row=1,
            column=2,
            sticky="e",
            pady=(self._theme.spacing.sm, 0),
        )

    def _build_alias_tab(self) -> None:
        tab = self._alias_tab
        spacing = self._theme.spacing
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(
            tab,
            style="Organize.Panel.TFrame",
            padding=(spacing.md, spacing.sm),
        )
        self._alias_toolbar = toolbar
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, spacing.sm))
        toolbar.columnconfigure(1, weight=1)
        title = ttk.Frame(toolbar, style="Organize.Surface.TFrame")
        self._alias_title = title
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(
            title,
            text="别名词典",
            style="Organize.Section.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._alias_title_hint = ttk.Label(
            title,
            text="统一角色、作品与人物的不同叫法",
            style="Organize.Hint.TLabel",
        )
        self._alias_title_hint.grid(
            row=0,
            column=1,
            sticky="w",
            padx=(spacing.md, 0),
        )
        self._alias_summary_label = ttk.Label(
            toolbar,
            textvariable=self._alias_summary_var,
            style="Organize.Metric.TLabel",
        )
        self._alias_summary_label.grid(
            row=0,
            column=1,
            sticky="e",
            padx=spacing.sm,
        )
        alias_actions = ttk.Frame(toolbar, style="Organize.Surface.TFrame")
        self._alias_actions = alias_actions
        alias_actions.grid(row=0, column=2, sticky="e")
        ttk.Button(
            alias_actions,
            text="刷新",
            command=self.refresh_aliases,
            style="Organize.Quiet.TButton",
        ).pack(side="left")
        ttk.Button(
            alias_actions,
            text="新建关系",
            command=self.new_alias,
            style="Organize.Secondary.TButton",
        ).pack(side="left", padx=(spacing.sm, 0))

        self._alias_workspace = ttk.Frame(tab, style="Organize.App.TFrame")
        self._alias_workspace.grid(row=1, column=0, sticky="nsew")
        list_frame = ttk.Frame(
            self._alias_workspace,
            style="Organize.Card.TFrame",
            padding=spacing.md,
        )
        edit_frame = ttk.Frame(
            self._alias_workspace,
            style="Organize.Card.TFrame",
            padding=spacing.md,
        )
        self._alias_list_card = list_frame
        self._alias_edit_card = edit_frame

        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)
        list_heading = ttk.Frame(list_frame, style="Organize.Surface.TFrame")
        list_heading.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, spacing.sm),
        )
        list_heading.columnconfigure(1, weight=1)
        ttk.Label(
            list_heading,
            text="已维护关系",
            style="Organize.Section.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            list_heading,
            text="选择一项可编辑或删除",
            style="Organize.Hint.TLabel",
        ).grid(row=0, column=1, sticky="e")
        self._alias_tree = ttk.Treeview(
            list_frame,
            columns=("canonical", "aliases"),
            show="headings",
            selectmode="browse",
            style="Organize.Treeview",
        )
        self._alias_tree.heading("canonical", text="规范名称")
        self._alias_tree.heading("aliases", text="别名")
        self._alias_tree.column("canonical", width=180)
        self._alias_tree.column("aliases", width=420)
        self._alias_tree_scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self._alias_tree.yview,
            style="Organize.Vertical.TScrollbar",
        )
        self._alias_tree.configure(yscrollcommand=self._alias_tree_scrollbar.set)
        self._alias_tree.grid(row=1, column=0, sticky="nsew")
        self._alias_tree_scrollbar.grid(row=1, column=1, sticky="ns")
        self._alias_tree.bind("<<TreeviewSelect>>", self._on_alias_select, add=True)

        self._alias_empty = ttk.Frame(
            list_frame,
            style="Organize.Empty.TFrame",
            padding=(spacing.xl, spacing.xl),
        )
        self._alias_empty.columnconfigure(0, weight=1)
        self._alias_empty.rowconfigure(0, weight=1)
        alias_empty_content = ttk.Frame(
            self._alias_empty,
            style="Organize.Empty.TFrame",
        )
        alias_empty_content.grid(row=0, column=0)
        ttk.Label(
            alias_empty_content,
            textvariable=self._alias_empty_title_var,
            style="Organize.EmptyTitle.TLabel",
        ).pack()
        ttk.Label(
            alias_empty_content,
            textvariable=self._alias_empty_hint_var,
            style="Organize.EmptyHint.TLabel",
            justify="center",
            wraplength=360,
        ).pack(pady=(spacing.xs, 0))
        self._alias_empty.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self._show_alias_empty(
            "别名词典尚未加载",
            "刷新后可维护角色、作品与人物的等价叫法",
        )

        edit_frame.columnconfigure(1, weight=1)
        ttk.Label(
            edit_frame,
            text="编辑别名关系",
            style="Organize.Section.TLabel",
        ).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, spacing.sm),
        )
        ttk.Label(
            edit_frame,
            text="规范名称",
            style="Organize.Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")
        self._alias_canonical_entry = ttk.Entry(
            edit_frame,
            textvariable=self._alias_canonical_var,
            style="Organize.TEntry",
        )
        self._alias_canonical_entry.grid(
            row=1, column=1, sticky="ew", padx=(7, 0), pady=3
        )
        ttk.Label(
            edit_frame,
            text="别名",
            style="Organize.Muted.TLabel",
        ).grid(row=2, column=0, sticky="w")
        ttk.Entry(
            edit_frame,
            textvariable=self._alias_values_var,
            style="Organize.TEntry",
        ).grid(row=2, column=1, sticky="ew", padx=(7, 0), pady=3)
        ttk.Label(
            edit_frame,
            text="可用空格、逗号或分号分隔，例如：雷神 影 巴尔泽布",
            style="Organize.Hint.TLabel",
            wraplength=360,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 9))
        buttons = ttk.Frame(edit_frame, style="Organize.Surface.TFrame")
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(
            buttons,
            text="删除关系",
            command=self.delete_alias,
            style="Organize.Danger.TButton",
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="保存关系",
            command=self.save_alias,
            style="Organize.Primary.TButton",
        ).pack(
            side="left",
            padx=(spacing.sm, 0),
        )

    def _on_review_canvas_configure(self, event: tk.Event[tk.Misc]) -> None:
        if self._closed:
            return
        self._review_canvas.itemconfigure(
            self._review_canvas_window,
            width=max(1, event.width),
        )
        self._refresh_review_scrollregion()

    def _on_review_content_configure(self, _event: tk.Event[tk.Misc]) -> None:
        if not self._closed:
            self._refresh_review_scrollregion()

    def _refresh_review_scrollregion(self) -> None:
        """Keep compact layouts scrollable without shrinking their controls."""

        if self._closed:
            return
        try:
            viewport_height = max(1, self._review_canvas.winfo_height())
            required_height = max(1, self._review_content.winfo_reqheight())
            target_height = max(viewport_height, required_height)
            self._review_canvas.itemconfigure(
                self._review_canvas_window,
                height=target_height,
            )
            width = max(1, self._review_canvas.winfo_width())
            self._review_canvas.configure(
                scrollregion=(0, 0, width, target_height),
            )
        except tk.TclError:
            if not self._closed:
                raise

    def _queue_review_scrollregion_refresh(self) -> None:
        if self._closed:
            return
        if self._layout_after_handle is not None:
            with suppress(tk.TclError):
                self.after_cancel(self._layout_after_handle)
        self._layout_after_handle = self.after_idle(
            self._run_queued_scrollregion_refresh
        )

    def _run_queued_scrollregion_refresh(self) -> None:
        self._layout_after_handle = None
        self._refresh_review_scrollregion()

    def _on_panel_configure(self, event: tk.Event[tk.Misc]) -> None:
        """Reflow page-local controls without touching review state."""

        if self._closed or event.widget is not self:
            return
        try:
            self._apply_responsive_layout(event.width)
        except tk.TclError:
            # Configure events may already be queued while a containing page is
            # being destroyed.  Ignore only that teardown race; live layout
            # failures remain visible to tests and callers.
            if not self._closed:
                raise

    def _apply_responsive_layout(self, width: int) -> None:
        self._assert_ui_thread()
        layout = calculate_organize_layout(width)
        if layout == self._responsive_layout:
            return
        spacing = self._theme.spacing

        if layout.filter_columns == 2:
            self._flow_steps.configure(
                text=(
                    "1  筛选队列   →   2  审核图片\n3  对比差异   →   4  批量或逐项提交"
                )
            )
        else:
            self._flow_steps.configure(
                text="1  筛选队列   →   2  审核图片   →   3  对比差异   →   "
                "4  批量或逐项提交"
            )
        if layout.compact_actions:
            self._flow_hint.grid_remove()
            self._shortcut_label.grid_remove()
            self._toolbar_hint.grid_remove()
            for hint in (
                self._filter_hint,
                self._review_list_hint,
                self._detail_hint,
            ):
                hint.grid_configure(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    padx=0,
                    pady=(spacing.xs, 0),
                )
            self._alias_title.grid_configure(columnspan=3, sticky="ew")
            self._alias_summary_label.grid_configure(
                row=1,
                column=0,
                sticky="w",
                padx=0,
                pady=(spacing.sm, 0),
            )
            self._alias_actions.grid_configure(
                row=1,
                column=2,
                sticky="e",
                pady=(spacing.sm, 0),
            )
            if layout.filter_columns == 2:
                self._alias_title_hint.grid_configure(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    padx=0,
                    pady=(spacing.xs, 0),
                )
            else:
                self._alias_title_hint.grid_configure(
                    row=0,
                    column=1,
                    columnspan=1,
                    sticky="w",
                    padx=(spacing.md, 0),
                    pady=0,
                )
        else:
            self._flow_hint.grid()
            self._shortcut_label.grid()
            self._toolbar_hint.grid()
            for hint in (
                self._filter_hint,
                self._toolbar_hint,
            ):
                hint.grid_configure(
                    row=0,
                    column=1,
                    columnspan=1,
                    sticky="w",
                    padx=(spacing.md, 0),
                    pady=0,
                )
            for hint in (self._review_list_hint, self._detail_hint):
                hint.grid_configure(
                    row=0,
                    column=1,
                    columnspan=1,
                    sticky="e",
                    padx=0,
                    pady=0,
                )
            self._alias_title.grid_configure(
                row=0,
                column=0,
                columnspan=1,
                sticky="w",
            )
            self._alias_title_hint.grid_configure(
                row=0,
                column=1,
                columnspan=1,
                sticky="w",
                padx=(spacing.md, 0),
                pady=0,
            )
            self._alias_summary_label.grid_configure(
                row=0,
                column=1,
                sticky="e",
                padx=spacing.sm,
                pady=0,
            )
            self._alias_actions.grid_configure(
                row=0,
                column=2,
                sticky="e",
                pady=0,
            )

        compact_button_text = layout.compact_actions
        self._previous_button.configure(
            text="上页 [" if compact_button_text else "上一页 ["
        )
        self._next_button.configure(
            text="下页 ]" if compact_button_text else "下一页 ]"
        )
        self._clear_selection_button.configure(
            text="清除" if compact_button_text else "清除选择"
        )
        self._batch_button.configure(
            text="批量接受" if compact_button_text else "批量接受低风险"
        )
        self._submit_button.configure(
            text="提交审核" if compact_button_text else "提交逐项审核"
        )
        self._undo_button.configure(
            text="撤销批量" if compact_button_text else "撤销最近批量"
        )

        for column in range(6):
            self._filter_grid.columnconfigure(
                column,
                weight=1 if column < layout.filter_columns else 0,
                uniform="organize-filter" if column < layout.filter_columns else "",
            )
        for index, field in enumerate(self._filter_items):
            field.grid_forget()
            column = index % layout.filter_columns
            field.grid(
                row=index // layout.filter_columns,
                column=column,
                sticky="ew",
                padx=(0, spacing.sm if column < layout.filter_columns - 1 else 0),
                pady=(0, spacing.sm),
            )

        for group in (
            self._navigation_actions,
            self._review_metrics,
            self._selection_actions,
            self._commit_actions,
        ):
            group.grid_forget()
        if layout.compact_actions:
            self._review_metrics.grid(
                row=1,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(spacing.sm, 0),
            )
            if layout.filter_columns >= 3:
                self._navigation_actions.grid(
                    row=2,
                    column=0,
                    sticky="w",
                    pady=(spacing.sm, 0),
                )
                self._commit_actions.grid(
                    row=2,
                    column=1,
                    sticky="e",
                    pady=(spacing.sm, 0),
                )
                self._selection_actions.grid(
                    row=3,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    pady=(spacing.sm, 0),
                )
            else:
                self._navigation_actions.grid(
                    row=2,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    pady=(spacing.sm, 0),
                )
                self._selection_actions.grid(
                    row=3,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    pady=(spacing.sm, 0),
                )
                self._commit_actions.grid(
                    row=4,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    pady=(spacing.sm, 0),
                )
        else:
            self._navigation_actions.grid(
                row=1,
                column=0,
                sticky="w",
                pady=(spacing.sm, 0),
            )
            self._review_metrics.grid(
                row=1,
                column=1,
                sticky="e",
                pady=(spacing.sm, 0),
            )
            self._selection_actions.grid(
                row=2,
                column=0,
                sticky="w",
                pady=(spacing.sm, 0),
            )
            self._commit_actions.grid(
                row=2,
                column=1,
                sticky="e",
                pady=(spacing.sm, 0),
            )

        self._layout_workspace(
            self._review_workspace,
            self._review_list_card,
            self._review_detail_card,
            stacked=layout.stacked_workspace,
            vertical_weights=(3, 4),
        )
        self._layout_workspace(
            self._alias_workspace,
            self._alias_list_card,
            self._alias_edit_card,
            stacked=layout.stacked_workspace,
            vertical_weights=(3, 2),
        )
        self._responsive_layout = layout
        self._queue_review_scrollregion_refresh()

    def _layout_workspace(
        self,
        workspace: ttk.Frame,
        primary: ttk.Widget,
        secondary: ttk.Widget,
        *,
        stacked: bool,
        vertical_weights: tuple[int, int],
    ) -> None:
        """Lay out one pair of cards horizontally or vertically."""

        spacing = self._theme.spacing
        primary.grid_forget()
        secondary.grid_forget()
        for index in (0, 1):
            workspace.columnconfigure(index, weight=0, uniform="")
            workspace.rowconfigure(index, weight=0, uniform="")
        if stacked:
            workspace.columnconfigure(0, weight=1)
            workspace.rowconfigure(0, weight=vertical_weights[0])
            workspace.rowconfigure(1, weight=vertical_weights[1])
            primary.grid(row=0, column=0, sticky="nsew")
            secondary.grid(
                row=1,
                column=0,
                sticky="nsew",
                pady=(spacing.sm, 0),
            )
            return
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, weight=3, uniform="organize-workspace")
        workspace.columnconfigure(1, weight=2, uniform="organize-workspace")
        primary.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, spacing.xs),
        )
        secondary.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=(spacing.xs, 0),
        )

    def _bind_shortcuts(self) -> None:
        bindings: tuple[tuple[str, Callable[[], None]], ...] = (
            ("<KeyPress-a>", self.mark_selected_accept),
            ("<KeyPress-A>", self.mark_selected_accept),
            ("<KeyPress-e>", self.mark_selected_edit),
            ("<KeyPress-E>", self.mark_selected_edit),
            ("<KeyPress-r>", self.mark_selected_reject),
            ("<KeyPress-R>", self.mark_selected_reject),
            ("<space>", self.toggle_selected_batch),
            ("<Control-a>", self.select_all_low_risk),
            ("<Control-Return>", self.submit_reviews),
            ("<Control-z>", self.undo_last_batch),
            ("<KeyPress-bracketleft>", self.previous_page),
            ("<KeyPress-bracketright>", self.next_page),
            ("<F5>", self.refresh),
        )
        for sequence, action in bindings:

            def handler(
                _event: tk.Event[tk.Misc],
                callback: Callable[[], None] = action,
            ) -> str:
                return self._keyboard(callback)

            self._review_tree.bind(
                sequence,
                handler,
                add=True,
            )

    @staticmethod
    def _keyboard(callback: Callable[[], None]) -> str:
        callback()
        return "break"

    def _show_review_empty(self, title: str, hint: str) -> None:
        """Replace the blank review table with a useful, actionable state."""

        self._review_empty_title_var.set(title)
        self._review_empty_hint_var.set(hint)
        self._review_tree.grid_remove()
        self._review_tree_scrollbar.grid_remove()
        self._review_empty.grid()

    def _show_review_rows(self) -> None:
        self._review_empty.grid_remove()
        self._review_tree.grid()
        self._review_tree_scrollbar.grid()

    def _show_alias_empty(self, title: str, hint: str) -> None:
        """Present loading and empty alias states without a blank data grid."""

        self._alias_empty_title_var.set(title)
        self._alias_empty_hint_var.set(hint)
        self._alias_tree.grid_remove()
        self._alias_tree_scrollbar.grid_remove()
        self._alias_empty.grid()

    def _show_alias_rows(self) -> None:
        self._alias_empty.grid_remove()
        self._alias_tree.grid()
        self._alias_tree_scrollbar.grid()

    def _has_active_filters(self) -> bool:
        filters = self._read_filters()
        return bool(
            filters.latest_index_only
            or filters.character
            or filters.work
            or filters.action
            or filters.expression
            or filters.review_state != "all"
        )

    def _read_filters(self) -> OrganizeFilters:
        selected_label = self._review_state_var.get()
        review_state = next(
            (value for label, value in _FILTER_STATES if label == selected_label),
            "all",
        )
        return OrganizeFilters(
            latest_index_only=self._latest_var.get(),
            character=self._character_var.get(),
            work=self._work_var.get(),
            action=self._action_var.get(),
            expression=self._expression_var.get(),
            review_state=review_state,
        )

    def _apply_pending_payload(self, payload: object, *, prefer_previous: bool) -> None:
        page = parse_pending_page(_unwrap_result(payload))
        if (
            prefer_previous
            and page.pagination.pending_count > 0
            and not page.proposals
            and page.pagination.offset > 0
        ):
            last_offset = page.pagination.last_valid_offset()
            if last_offset != page.pagination.offset:
                self.refresh(
                    offset=last_offset,
                    prefer_previous=False,
                    allow_discard=True,
                )
                return
        self._page = page
        self._session = OrganizeReviewSession(page)
        self._populate_review_tree()
        count = page.pagination.pending_count
        self._set_status(
            "当前没有待审核建议"
            if count == 0
            else f"已加载 {len(page.proposals)} 张，图库共有 {count} 张待审核"
        )

    def _populate_review_tree(self) -> None:
        self._assert_ui_thread()
        self._row_to_proposal.clear()
        self._proposal_to_row.clear()
        for row in self._review_tree.get_children():
            self._review_tree.delete(row)
        page = self._page
        if page is None:
            return
        for index, proposal in enumerate(page.proposals):
            row = f"proposal_row_{index}"
            self._row_to_proposal[row] = proposal.proposal_id
            self._proposal_to_row[proposal.proposal_id] = row
            self._review_tree.insert(
                "",
                "end",
                iid=row,
                values=self._row_values(proposal),
                tags=(self._row_visual_tag(proposal),),
            )
        if page.proposals:
            self._show_review_rows()
            first_row = self._proposal_to_row[page.proposals[0].proposal_id]
            self._review_tree.selection_set(first_row)
            self._review_tree.focus(first_row)
            self._review_tree.see(first_row)
            self._selected_proposal_id = page.proposals[0].proposal_id
        else:
            self._selected_proposal_id = None
            if self._has_active_filters():
                self._show_review_empty(
                    "没有符合筛选条件的建议",
                    "可以清除部分筛选条件，或刷新队列查看最新建议",
                )
            else:
                self._show_review_empty(
                    "当前没有待审核建议",
                    "新的模型建议生成后会自动出现在这里",
                )
        self._issue_var.set(
            ""
            if not page.issues
            else f"已隔离 {len(page.issues)} 条损坏建议；其余图片可继续审核。"
        )
        self._update_page_state()
        self._render_selected()

    def _row_values(self, proposal: OrganizeProposal) -> tuple[str, ...]:
        session = self._require_session()
        state = session.state_for(proposal.proposal_id)
        batch = (
            "☑" if state.batch_selected else "☐" if proposal.can_batch_accept else "—"
        )
        status = self._proposal_state_label(proposal)
        if state.decision != "pending":
            status = {
                "accept": "接受",
                "edit": "编辑",
                "reject": "拒绝",
                "manual": "手工补标",
            }.get(state.decision, status)
        low = " · ".join(proposal.batch_safe_tags) or "—"
        identities = " · ".join(proposal.identity_tags) or "—"
        difference = (
            f"保留 {len(proposal.existing_tags)} / "
            f"新增 {len(proposal.difference.suggested_additions)}"
        )
        return batch, status, proposal.relative_path, low, identities, difference

    def _row_visual_tag(self, proposal: OrganizeProposal) -> str:
        """Map review semantics to restrained colour cues in the queue."""

        session = self._require_session()
        decision = session.state_for(proposal.proposal_id).decision
        if decision == "accept":
            return "accepted"
        if decision in {"edit", "manual"}:
            return "edited"
        if decision == "reject":
            return "rejected"
        if "failed" in proposal.review_buckets or "conflict" in proposal.review_buckets:
            return "failed"
        if "identity" in proposal.review_buckets:
            return "identity"
        if "low_risk" in proposal.review_buckets:
            return "low_risk"
        return ""

    @staticmethod
    def _proposal_state_label(proposal: OrganizeProposal) -> str:
        for state in ("failed", "conflict", "identity", "low_risk", "pending"):
            if state in proposal.review_buckets:
                return _STATE_LABELS[state]
        return "待审核"

    def _on_review_select(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._commit_editor(silent=True)
        selection = self._review_tree.selection()
        if not selection:
            return
        proposal_id = self._row_to_proposal.get(selection[0])
        if proposal_id is None:
            return
        self._selected_proposal_id = proposal_id
        self._render_selected()

    def _render_selected(self, *, keep_editor: bool = False) -> None:
        self._assert_ui_thread()
        proposal = self._selected_proposal(optional=True)
        for row in self._source_tree.get_children():
            self._source_tree.delete(row)
        for child in self._identity_frame.winfo_children():
            child.destroy()
        self._identity_variables.clear()
        if proposal is None or self._session is None:
            self._identity_frame.configure(style="Organize.Editor.TFrame")
            self._preview.set_image(None)
            self._path_var.set("请选择待审核图片")
            self._description_var.set("")
            self._existing_var.set("现有标签：—")
            self._suggested_var.set("建议标签：—")
            self._difference_var.set("差异：—")
            self._decision_var.set("状态：待审核")
            self._edited_tags_var.set("")
            self._batch_var.set(False)
            self._update_controls()
            return
        state = self._session.state_for(proposal.proposal_id)
        self._preview.set_image(proposal.source_path)
        self._path_var.set(proposal.relative_path)
        self._description_var.set(proposal.description or proposal.error)
        self._existing_var.set(
            "现有标签：" + (" · ".join(proposal.existing_tags) or "—")
        )
        self._suggested_var.set(
            "建议标签：" + (" · ".join(proposal.proposed_tags) or "—")
        )
        self._difference_var.set(
            "差异：低风险新增 "
            + (" · ".join(proposal.batch_safe_tags) or "—")
            + "；身份待确认 "
            + (" · ".join(proposal.difference.identity_additions) or "—")
        )
        if not keep_editor:
            self._synchronizing_editor = True
            self._edited_tags_var.set(" ".join(state.edited_tags))
            self._synchronizing_editor = False
        self._batch_var.set(state.batch_selected)
        self._decision_var.set(
            "状态："
            + {
                "pending": "待审核",
                "accept": "接受",
                "edit": "编辑后接受",
                "reject": "拒绝",
                "manual": "手工补标",
            }[state.decision]
        )
        for detail in proposal.tag_details:
            source_state = (
                "已有"
                if detail.already_present
                else "身份待确认"
                if detail.is_identity
                else "低风险新增"
                if detail.is_low_risk
                else "建议"
            )
            confidence = "" if detail.confidence is None else f"{detail.confidence:.0%}"
            self._source_tree.insert(
                "",
                "end",
                values=(
                    detail.tag,
                    detail.effective_source_label,
                    source_state,
                    confidence,
                ),
            )
        if proposal.identity_choices:
            self._identity_frame.configure(style="Organize.Identity.TFrame")
            ttk.Label(
                self._identity_frame,
                text="身份标签必须逐项确认，未勾选的身份不会写入：",
                style="Organize.IdentityWarning.TLabel",
            ).pack(anchor="w")
            for choice in proposal.identity_choices:
                variable = tk.BooleanVar(
                    value=choice.tag in state.confirmed_identity_tags
                )
                self._identity_variables[choice.tag] = variable

                def toggle_identity(
                    tag: str = choice.tag,
                    current: tk.BooleanVar = variable,
                ) -> None:
                    self.confirm_selected_identity(tag, current.get())

                ttk.Checkbutton(
                    self._identity_frame,
                    text=f"{_entity_label(choice.entity_type)}：{choice.tag}",
                    variable=variable,
                    command=toggle_identity,
                    style="Organize.Identity.TCheckbutton",
                ).pack(anchor="w", pady=1)
        else:
            self._identity_frame.configure(style="Organize.Editor.TFrame")
            ttk.Label(
                self._identity_frame,
                text="无待确认身份标签",
                style="Organize.CommandHint.TLabel",
            ).pack(anchor="w")
        self._update_controls()

    def _mark_selected(self, decision: ReviewDecision) -> None:
        self._assert_ui_thread()
        proposal_id = self._require_selected_id()
        session = self._require_session()
        self._commit_editor()
        try:
            session.set_decision(proposal_id, decision)
        except OrganizeStateError as exc:
            self._show_error("无法更新审核状态", exc)
            return
        self._update_row(proposal_id)
        self._render_selected(keep_editor=True)
        self._move_selection(1)

    def _commit_editor(self, *, silent: bool = False) -> None:
        if self._synchronizing_editor or self._selected_proposal_id is None:
            return
        if self._session is None:
            return
        try:
            self._session.set_edited_tags(
                self._selected_proposal_id,
                parse_tag_text(self._edited_tags_var.get()),
            )
        except OrganizeStateError as exc:
            if not silent:
                self._show_error("标签格式无效", exc)

    def _on_editor_return(self, _event: tk.Event[tk.Misc]) -> str:
        self.mark_selected_edit()
        return "break"

    def _on_editor_focus_out(self, _event: tk.Event[tk.Misc]) -> None:
        self._commit_editor(silent=True)

    def _on_batch_toggle(self) -> None:
        proposal_id = self._require_selected_id()
        session = self._require_session()
        try:
            session.select_low_risk(
                proposal_id,
                selected=self._batch_var.get(),
            )
        except OrganizeStateError as exc:
            self._batch_var.set(False)
            self._show_error("无法加入批量审核", exc)
            return
        self._update_row(proposal_id)
        self._update_page_state()

    def _move_selection(self, delta: int) -> None:
        children = self._review_tree.get_children()
        if not children or self._selected_proposal_id is None:
            return
        row = self._proposal_to_row.get(self._selected_proposal_id)
        if row not in children:
            return
        index = children.index(row)
        target = children[max(0, min(len(children) - 1, index + delta))]
        self._review_tree.selection_set(target)
        self._review_tree.focus(target)
        self._review_tree.see(target)
        proposal_id = self._row_to_proposal[target]
        self._selected_proposal_id = proposal_id
        self._render_selected()

    def _update_row(self, proposal_id: str) -> None:
        row = self._proposal_to_row.get(proposal_id)
        proposal = self._proposal_by_id(proposal_id)
        if row is not None and proposal is not None:
            self._review_tree.item(
                row,
                values=self._row_values(proposal),
                tags=(self._row_visual_tag(proposal),),
            )
        self._update_page_state()

    def _refresh_all_rows(self) -> None:
        if self._page is None:
            return
        for proposal in self._page.proposals:
            self._update_row(proposal.proposal_id)

    def _update_page_state(self) -> None:
        page = self._page
        session = self._session
        if page is None or session is None:
            self._pending_var.set("待审核 0 张")
            self._page_var.set("第 0 / 0 页")
            self._progress_var.set("已审核 0 / 100")
            self._selection_var.set("低风险已选 0 张")
        else:
            pagination = page.pagination
            self._pending_var.set(f"待审核 {pagination.pending_count} 张")
            self._page_var.set(
                f"第 {pagination.current_page} / {pagination.page_count} 页"
            )
            progress = session.progress
            self._progress_var.set(
                f"已审核 {progress.reviewed_items} / {progress.target_items}"
            )
            self._selection_var.set(f"低风险已选 {progress.batch_selected_items} 张")
        self._update_controls()

    def _update_controls(self) -> None:
        page = self._page
        session = self._session
        busy = bool(self._busy_operations)
        self._refresh_button.configure(state="disabled" if busy else "normal")
        self._previous_button.configure(
            state=(
                "normal"
                if not busy
                and page is not None
                and page.pagination.previous_offset is not None
                else "disabled"
            )
        )
        self._next_button.configure(
            state=(
                "normal"
                if not busy
                and page is not None
                and page.pagination.next_offset is not None
                else "disabled"
            )
        )
        has_items = bool(page and page.proposals)
        selected_count = 0 if session is None else session.progress.batch_selected_items
        self._select_all_button.configure(
            state="normal" if not busy and has_items else "disabled"
        )
        self._clear_selection_button.configure(
            state="normal" if not busy and selected_count else "disabled"
        )
        self._batch_button.configure(
            state="normal" if not busy and selected_count else "disabled"
        )
        self._submit_button.configure(
            state=(
                "normal"
                if not busy and session is not None and session.has_unsaved_changes
                else "disabled"
            )
        )
        self._undo_button.configure(
            state=(
                "normal"
                if not busy and session is not None and session.undo_available
                else "disabled"
            )
        )
        proposal = self._selected_proposal(optional=True)
        has_selected = proposal is not None
        decision_state = "normal" if not busy and has_selected else "disabled"
        self._accept_button.configure(state=decision_state)
        self._edit_button.configure(state=decision_state)
        self._reject_button.configure(state=decision_state)
        self._edited_tags_entry.configure(state=decision_state)
        self._batch_check.configure(
            state=(
                "normal"
                if not busy and proposal is not None and proposal.can_batch_accept
                else "disabled"
            )
        )
        confirmed = (
            False
            if proposal is None or session is None
            else bool(session.state_for(proposal.proposal_id).confirmed_identity_tags)
        )
        self._identity_submit_button.configure(
            state="normal" if not busy and confirmed else "disabled"
        )

    def _after_review(self, payload: object, message: str) -> None:
        result = parse_review_result(_unwrap_result(payload))
        detail = (
            f"{message}：接受 {result.accepted}，拒绝 {result.rejected}，"
            f"失败 {result.failed}"
        )
        self._set_status(detail)
        offset = 0 if self._page is None else self._page.pagination.offset
        self.refresh(
            offset=offset,
            prefer_previous=True,
            allow_discard=True,
        )

    def _after_batch(self, payload: object) -> None:
        session = self._require_session()
        result = session.record_batch_result(_unwrap_result(payload))
        self._set_status(
            f"批量更新 {result.updated} 张，已排除身份标签 "
            f"{result.identity_excluded_count} 项；可撤销最近一次操作"
        )
        offset = 0 if self._page is None else self._page.pagination.offset
        self.refresh(
            offset=offset,
            prefer_previous=True,
            allow_discard=True,
        )

    def _after_undo(self, payload: object) -> None:
        session = self._require_session()
        result = session.record_undo_result(_unwrap_result(payload))
        self._set_status(
            f"已恢复 {result.updated} 张图片"
            if result.undone
            else "后端没有找到可撤销的批量操作"
        )
        self.refresh(
            offset=0,
            prefer_previous=False,
            allow_discard=True,
        )

    def _apply_alias_payload(self, payload: object) -> None:
        self._aliases = parse_alias_catalog(_unwrap_result(payload))
        self._aliases_loaded = True
        self._populate_alias_tree()
        self._set_status(f"已加载 {self._aliases.count} 组别名关系")

    def _after_alias_mutation(self, payload: object) -> None:
        result = parse_alias_mutation_result(_unwrap_result(payload))
        self._set_status(
            "别名关系已保存"
            if result.updated
            else "别名关系已删除"
            if result.deleted
            else "别名关系没有变化"
        )
        self.refresh_aliases()

    def _populate_alias_tree(self) -> None:
        for row in self._alias_tree.get_children():
            self._alias_tree.delete(row)
        for index, entry in enumerate(self._aliases.entries):
            self._alias_tree.insert(
                "",
                "end",
                iid=f"alias_row_{index}",
                values=(entry.canonical_name, " · ".join(entry.aliases)),
            )
        self._alias_summary_var.set(f"共 {self._aliases.count} 组关系")
        if self._aliases.entries:
            self._show_alias_rows()
        else:
            self._show_alias_empty(
                "还没有别名关系",
                "点击“新建关系”，统一同一角色、作品或人物的不同叫法",
            )

    def _on_alias_select(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selection = self._alias_tree.selection()
        if not selection:
            return
        values = self._alias_tree.item(selection[0], "values")
        if not isinstance(values, (tuple, list)) or len(values) < 2:
            return
        self._alias_canonical_var.set(str(values[0]))
        entry = next(
            (
                item
                for item in self._aliases.entries
                if item.canonical_name == str(values[0])
            ),
            None,
        )
        self._alias_values_var.set(" ".join(entry.aliases) if entry else "")

    def _on_tab_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._closed or self._aliases_loaded:
            return
        try:
            current = self._notebook.index(self._notebook.select())
        except tk.TclError:
            return
        if current == 1:
            self.refresh_aliases()

    def _open_selected_image(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        proposal = self._selected_proposal(optional=True)
        if (
            proposal is not None
            and proposal.source_path is not None
            and self._on_open_image is not None
        ):
            self._on_open_image(proposal.source_path)

    def _start_operation(
        self,
        operation: str,
        future: AsyncResult,
        callback: Callable[[object], None],
    ) -> None:
        if not isinstance(future, Future):
            raise TypeError(f"{operation} must return concurrent.futures.Future")
        token = self._operation_tokens.get(operation, 0) + 1
        self._operation_tokens[operation] = token
        self._busy_operations.add(operation)
        self._update_controls()

        def completed(value: AsyncResult) -> None:
            self._completion_queue.put(_Completion(operation, token, value, callback))

        future.add_done_callback(completed)

    def _drain_completions(self) -> None:
        self._after_handle = None
        if self._closed:
            return
        self._assert_ui_thread()
        for _ in range(128):
            try:
                item = self._completion_queue.get_nowait()
            except queue.Empty:
                break
            if self._operation_tokens.get(item.operation) != item.token:
                continue
            self._busy_operations.discard(item.operation)
            try:
                payload = item.future.result()
                item.callback(payload)
            except Exception as exc:
                if item.operation == "pending" and self._page is None:
                    self._show_review_empty(
                        "审核队列加载失败",
                        "请检查后端状态后点击刷新，已完成的审核不会丢失",
                    )
                elif item.operation == "alias_list" and not self._aliases_loaded:
                    self._show_alias_empty(
                        "别名词典加载失败",
                        "请检查后端状态后重试，现有别名关系不会被修改",
                    )
                self._show_error(_operation_error_title(item.operation), exc)
            finally:
                self._update_controls()
        if not self._closed:
            with suppress(tk.TclError):
                self._after_handle = self.after(25, self._drain_completions)

    def _set_status(self, message: str) -> None:
        self._assert_ui_thread()
        self._status_var.set(message)

    def _confirm_discard_changes(self) -> bool:
        session = self._session
        if session is None or not session.has_unsaved_changes:
            return True
        return self._confirm(
            "放弃未提交修改",
            "当前页面有尚未提交的审核选择。切换、筛选或刷新后这些修改会丢失。"
            "是否继续？",
        )

    def _show_error(self, title: str, error: BaseException | str) -> None:
        self._assert_ui_thread()
        message = str(error)
        if isinstance(error, (OrganizeDataError, OrganizeValidationError)):
            message = error.to_dict()["message"]
        self._status_var.set(f"{title}：{message}")
        self._report_error(title, message)

    def _assert_ui_thread(self) -> None:
        current = threading.get_ident()
        if current != self._ui_thread_id:
            raise RuntimeError("Tk state must be updated on the creating thread")
        self._last_ui_update_thread_id = current

    def _require_session(self) -> OrganizeReviewSession:
        if self._session is None:
            raise OrganizeValidationError(
                "请先加载待审核建议。",
                code="review_page_not_loaded",
            )
        return self._session

    def _require_selected_id(self) -> str:
        if self._selected_proposal_id is None:
            raise OrganizeValidationError(
                "请先选择一张图片。",
                code="proposal_not_selected",
            )
        return self._selected_proposal_id

    def _selected_proposal(self, *, optional: bool = False) -> OrganizeProposal | None:
        if self._selected_proposal_id is None:
            if optional:
                return None
            raise OrganizeValidationError(
                "请先选择一张图片。",
                code="proposal_not_selected",
            )
        proposal = self._proposal_by_id(self._selected_proposal_id)
        if proposal is None and not optional:
            raise OrganizeValidationError(
                "当前选择已经失效，请刷新。",
                code="proposal_not_found",
            )
        return proposal

    def _proposal_by_id(self, proposal_id: str) -> OrganizeProposal | None:
        if self._page is None:
            return None
        return next(
            (item for item in self._page.proposals if item.proposal_id == proposal_id),
            None,
        )

    def _clear_review_widgets(self) -> None:
        for row in self._review_tree.get_children():
            self._review_tree.delete(row)
        self._selected_proposal_id = None
        self._show_review_empty(
            "审核队列尚未加载",
            "刷新后会在这里显示待审核的图片建议",
        )
        self._render_selected()
        self._update_page_state()

    def _clear_alias_widgets(self) -> None:
        for row in self._alias_tree.get_children():
            self._alias_tree.delete(row)
        self._alias_canonical_var.set("")
        self._alias_values_var.set("")
        self._alias_summary_var.set("别名词典尚未加载")
        self._show_alias_empty(
            "别名词典尚未加载",
            "刷新后可维护角色、作品与人物的等价叫法",
        )

    def _default_confirm(self, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title, message, parent=self.winfo_toplevel()))

    def _default_report_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self.winfo_toplevel())


def _default_thumbnail_factory(
    master: tk.Misc,
    dispatcher: ImageTaskDispatcher,
    theme: DesktopTheme,
) -> AsyncImageCanvas:
    return AsyncImageCanvas(
        master,
        dispatcher,
        mode="contain",
        background=theme.surface_muted,
        placeholder="暂无可用缩略图",
    )


def _unwrap_result(payload: object) -> object:
    if not isinstance(payload, Mapping) or "status" not in payload:
        return payload
    status = payload.get("status")
    if not isinstance(status, str):
        raise OrganizeDataError(
            "Backend job status must be a string.",
            path="job.status",
        )
    normalized = status.strip().lower()
    if normalized not in _SUCCESS_JOB_STATUSES:
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            code = error.get("code")
            raise OrganizeDataError(
                str(message or f"Backend job ended as {normalized}."),
                path="job.error",
                code=str(code or "backend_job_failed"),
                details={"status": normalized, "error": dict(error)},
            )
        raise OrganizeDataError(
            f"Backend job ended as {normalized}.",
            path="job.status",
            code="backend_job_failed",
            details={"status": normalized},
        )
    if "result" not in payload or not isinstance(payload["result"], Mapping):
        raise OrganizeDataError(
            "Completed backend job did not contain a result object.",
            path="job.result",
        )
    return payload["result"]


def _entity_label(value: str) -> str:
    return {
        "real_person": "真人",
        "cosplayer": "Cosplayer",
        "character": "角色",
        "work": "作品",
    }.get(value, "身份")


def _operation_error_title(operation: str) -> str:
    return {
        "pending": "无法加载待审核建议",
        "review": "无法提交审核",
        "identity_review": "无法提交身份确认",
        "batch": "无法批量接受标签",
        "undo": "无法撤销批量操作",
        "alias_list": "无法加载别名词典",
        "alias_mutation": "无法修改别名词典",
    }.get(operation, "操作失败")


__all__ = [
    "AsyncResult",
    "ConfirmCallback",
    "ErrorCallback",
    "OpenImageCallback",
    "OrganizeAsyncOperations",
    "OrganizePanel",
    "ThumbnailFactory",
    "parse_tag_text",
]
