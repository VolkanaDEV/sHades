"""Preset palettes for the Kivy interface; no Kivy imports required."""

PRESETS = {
    "Midnight": {},
    "Ember": {"bg_main": "#1B1113", "panel": "#281C20",
              "panel_darker": "#302024", "bg_sidebar": "#21171B",
              "topbar": "#281C20", "primary": "#FFAE70",
              "accent": "#FF8F92", "sidebar_ctrl": "#3B292E",
              "msg_own": "#38282A", "input_bg": "#36262A",
              "input_bg_focus": "#493036"},
    "Mint": {"bg_main": "#081816", "panel": "#102421",
             "panel_darker": "#17312C", "bg_sidebar": "#0B1D1A",
             "topbar": "#102421", "primary": "#80E2BB",
             "accent": "#B9F3DC", "sidebar_ctrl": "#1D3C33",
             "msg_own": "#1E403A", "input_bg": "#1B3630",
             "input_bg_focus": "#285046"},
    "Mono": {"bg_main": "#101318", "panel": "#1C2026",
             "panel_darker": "#242A31", "bg_sidebar": "#171B20",
             "topbar": "#1C2026", "primary": "#E0E6EB",
             "accent": "#AAB8C2", "sidebar_ctrl": "#323942",
             "msg_own": "#303841", "input_bg": "#2A3139",
             "input_bg_focus": "#38434E", "btn_text": "#101318"},
    "Daylight": {"bg_main": "#F2F5F4", "panel": "#FFFFFF",
                 "panel_darker": "#EAF1EF", "bg_sidebar": "#E6EEEB",
                 "topbar": "#F5FAF8", "primary": "#146D65",
                 "accent": "#245C91", "sidebar_ctrl": "#D5E7E1",
                 "msg_own": "#DCEDE9", "input_bg": "#EAF1EF",
                 "input_bg_focus": "#D5E7E1", "text": "#15282C",
                 "text_muted": "#415D61", "text_dim": "#5B7073",
                 "btn_text": "#FFFFFF", "border": "#729B96",
                 "border_subtle": "#C7DBD6"},
}


def preset_colors(base, name):
    """Return a complete copy; invalid names leave the current palette alone."""
    if name not in PRESETS:
        raise ValueError("unknown theme preset")
    colors = dict(base)
    for key, value in PRESETS[name].items():
        colors[key] = tuple(int(value[i:i + 2], 16) / 255
                            for i in (1, 3, 5)) + (1.0,)
    if name == "Daylight":
        colors.update(aurora_a=(0.22, 0.65, 0.55, 0.11),
                      aurora_b=(0.83, 0.65, 0.36, 0.10),
                      aurora_c=(0.35, 0.50, 0.75, 0.08),
                      glass=(0.20, 0.40, 0.36, 0.045),
                      glass_edge=(0.12, 0.38, 0.34, 0.16))
    elif name == "Mono":
        colors.update(aurora_a=(0.55, 0.60, 0.65, 0.10),
                      aurora_b=(0.35, 0.40, 0.45, 0.10),
                      aurora_c=(0.60, 0.65, 0.70, 0.08))
    elif name == "Mint":
        colors.update(aurora_a=(0.35, 0.84, 0.70, 0.18),
                      aurora_b=(0.42, 0.93, 0.82, 0.13),
                      aurora_c=(0.30, 0.58, 0.57, 0.12))
    elif name == "Ember":
        colors.update(aurora_a=(1.0, 0.47, 0.34, 0.21),
                      aurora_b=(1.0, 0.72, 0.45, 0.13),
                      aurora_c=(0.75, 0.35, 0.45, 0.13))
    return colors
