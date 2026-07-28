from dataclasses import dataclass, field

@dataclass
class Config:
    # ── Account ───────────────────────────────────────────
    initial_capital:           float = 1878.0
    reserve_usd:               float = 300.0

    # ── Pairs ─────────────────────────────────────────────
    active_pairs: list = field(default_factory=lambda: [
        "BTC-USDT", "ETH-USDT", "SOL-USDT",
        "XRP-USDT", "LINK-USDT", "SUI-USDT",
    ])

    # ── Grid Bot ──────────────────────────────────────────
    grid_levels:               int   = 6
    neutral_grid_levels:       int   = 4
    atr_spacing_mult:          float = 0.20
    min_spacing_pct:           float = 0.0015
    min_atr_pct:               float = 0.0035
    atr_period:                int   = 14
    atr_bar:                   str   = "1H"
    max_leverage:              int   = 2
    max_notional_usd:          float = 500.0
    notional_pct:              float = 0.267  # 26.7% of equity per order
    grid_margin_usd:           float = 250.0
    neutral_notional_scale:    float = 1.0
    max_open_pairs:            int   = 6
    max_new_entries_per_scan:  int   = 2

    # ── Trend Bot ─────────────────────────────────────────
    trend_leverage:            int   = 3
    trend_margin_pct:          float = 0.15
    trend_max_slots:           int   = 2
    ema_short:                 int   = 20
    ema_long:                  int   = 50
    ema_period:                int   = 50
    ema_threshold:             float = 0.008
    trend_vol_min:             float = 0.50
    trend_entry_offset:        float = 0.0005
    trend_tp_pct:              float = 0.025
    trend_sl_pct:              float = 0.010

    # ── TP / SL ───────────────────────────────────────────
    atr_tp_mult:               float = 2.0
    atr_sl_mult:               float = 1.0
    min_tp_pct:                float = 0.003
    max_loss_usd:              float = 20.0
    max_total_open_loss_usd:   float = 66.0
    max_position_notional_usd: float = 550.0
    max_dd_pct:                float = 0.04

    # ── Volume Filters ────────────────────────────────────
    min_volume_24h_usd:        float = 300000.0
    vol_drop_threshold:        float = 0.25

    # ── Timing ────────────────────────────────────────────
    scan_interval:             int   = 30
    watch_interval:            int   = 5
    cooldown_after_close_s:    int   = 300

config = Config()
