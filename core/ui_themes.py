"""Single source of truth for Atelier themes and decoupled interactive FX presets.

Kept isolated from core business logic so upstream merges on core/settings.py
and app.py stay minimal and conflict-free.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemeSpec:
    key: str
    title: str
    subtitle: str
    full_label: str
    scheme: str       # "light" | "dark"
    group: str        # "eyecare" | "style"
    default_fx: str   # "calm" | "pixel" | "cyber" | "anime"
    swatch_canvas: str
    swatch_card: str
    swatch_accent: str


@dataclass(frozen=True)
class FxPresetSpec:
    key: str
    title: str
    subtitle: str
    icon: str


THEME_SPECS: tuple[ThemeSpec, ...] = (
    ThemeSpec("sage", "青岚", "晴竹护眼", "青岚 · 晴竹护眼", "light", "eyecare", "calm", "#EDF4F0", "#F7FBF8", "#146652"),
    ThemeSpec("mint", "青柚", "薄荷清风", "青柚 · 薄荷清风", "light", "eyecare", "calm", "#EBF5F5", "#F6FCFC", "#0E6370"),
    ThemeSpec("paper", "暖宣", "晨曦护眼", "暖宣 · 晨曦护眼", "light", "eyecare", "calm", "#F5F2EA", "#FCFBF7", "#1C586E"),
    ThemeSpec("linen", "晴空", "晨雾护眼", "晴空 · 晨雾护眼", "light", "eyecare", "calm", "#F1F5F8", "#FAFBFD", "#1F5494"),
    ThemeSpec("apricot", "暖杏", "柔光护眼", "暖杏 · 柔光护眼", "light", "eyecare", "calm", "#F6EFE8", "#FCF8F4", "#853D29"),
    ThemeSpec("night", "星夜", "柔暗护眼", "星夜 · 柔暗护眼", "dark", "eyecare", "calm", "#13171C", "#1D232B", "#7AC7DC"),
    ThemeSpec("pixel", "像素", "复古掌机", "像素 · 复古掌机", "light", "style", "pixel", "#EDF1E3", "#F8FAF1", "#263C8C"),
    ThemeSpec("cyber", "科技", "霓虹终端", "科技 · 霓虹终端", "dark", "style", "cyber", "#0D131D", "#162030", "#5CE1F5"),
    ThemeSpec("anime", "二次元", "樱风绘卷", "二次元 · 樱风绘卷", "light", "style", "anime", "#F8F1F5", "#FDF9FB", "#A8265E"),
)

FX_PRESET_SPECS: tuple[FxPresetSpec, ...] = (
    FxPresetSpec("auto", "自动跟随主题", "按当前主题自动匹配最协调的动效", "bi-magic"),
    FxPresetSpec("calm", "清新柔动", "轻盈浮起、水波涟漪与柔和换页过渡", "bi-wind"),
    FxPresetSpec("pixel", "像素街机", "阶梯帧按压反馈与 8-bit 像素方块粒子", "bi-grid-3x3-gap"),
    FxPresetSpec("cyber", "科技终端", "HUD 角标亮框、光束扫过与脉冲光环", "bi-cpu"),
    FxPresetSpec("anime", "二次元樱风", "弹性跃动、樱瓣星芒微爆与轻快切页", "bi-stars"),
)

THEME_BY_KEY: dict[str, ThemeSpec] = {spec.key: spec for spec in THEME_SPECS}
THEME_ORDER: tuple[str, ...] = tuple(spec.key for spec in THEME_SPECS)
UI_THEMES: set[str] = set(THEME_ORDER)
DARK_THEMES: set[str] = {spec.key for spec in THEME_SPECS if spec.scheme == "dark"}
DEFAULT_UI_THEME: str = "sage"

FX_PRESET_ORDER: tuple[str, ...] = tuple(spec.key for spec in FX_PRESET_SPECS)
UI_FX_PRESETS: set[str] = set(FX_PRESET_ORDER)
DEFAULT_UI_FX_ENABLED: str = "true"
DEFAULT_UI_FX_PRESET: str = "auto"


def normalize_theme(value: object) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned in UI_THEMES:
            return cleaned
    return DEFAULT_UI_THEME


def normalize_fx_preset(value: object) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned in UI_FX_PRESETS:
            return cleaned
    return DEFAULT_UI_FX_PRESET


def normalize_fx_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() != "false"
    return True


def is_dark_theme(theme: str) -> bool:
    return theme in DARK_THEMES


def resolve_fx(theme: str, preset: str, enabled: bool) -> str:
    """Resolve the active CSS/JS FX mode (`none|calm|pixel|cyber|anime`)."""
    if not enabled:
        return "none"
    normalized_preset = normalize_fx_preset(preset)
    if normalized_preset == "auto":
        spec = THEME_BY_KEY.get(normalize_theme(theme))
        return spec.default_fx if spec else "calm"
    return normalized_preset
