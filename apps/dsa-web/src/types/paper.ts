export interface PaperPosition {
  symbol: string;
  quantity: number;
  avgCost: number;
  lastPrice: number;
  unrealizedPnl: number;
}

export interface PaperCurvePoint {
  date: string;
  totalEquity: number;
  totalCash: number;
  marketValue: number;
}

export interface PaperTradeItem {
  id?: number;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  fee?: number;
  tax?: number;
  tradeDate?: string;
  note?: string;
}

export interface PaperSnapshot {
  totalCash?: number;
  totalMarketValue?: number;
  totalEquity?: number;
  unrealizedPnl?: number;
  realizedPnl?: number;
}

export interface PaperRiskConfig {
  stopLossPct?: number;
  takeProfitPct?: number;
  trailingActivatePct?: number;
  trailingDrawdownPct?: number;
}

export interface PaperDashboard {
  initialized: boolean;
  message?: string;
  asOf?: string;
  initialCash?: number;
  returnPct?: number;
  snapshot?: PaperSnapshot;
  positions: PaperPosition[];
  curve: PaperCurvePoint[];
  trades: PaperTradeItem[];
  risk?: PaperRiskConfig;
}
