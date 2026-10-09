export interface PickVerificationData {
  verifyDate?: string;
  evalStatus: string;
  closePrice?: number | null;
  returnPct?: number | null;
  benchmarkCode?: string | null;
  benchmarkReturnPct?: number | null;
  excessReturnPct?: number | null;
  isUp?: boolean | null;
  beatBenchmark?: boolean | null;
  metExpectation?: boolean | null;
  errorMessage?: string | null;
}

export interface ScreeningPickItem {
  code: string;
  name?: string | null;
  rank: number;
  market: string;
  strategy?: string | null;
  entryPrice?: number | null;
  finalScore?: number | null;
  rankingReason?: string | null;
  verification?: PickVerificationData | null;
}

export interface PickMarketGroup {
  market: string;
  picks: ScreeningPickItem[];
}

export interface PickDayGroup {
  pickDate: string;
  markets: Record<string, PickMarketGroup>;
  totalCount: number;
  verifiedCount: number;
  metCount: number;
  upCount: number;
  hitRatePct?: number | null;
  winRatePct?: number | null;
  avgReturnPct?: number | null;
  avgExcessReturnPct?: number | null;
}

export interface PickVerificationSummary {
  totalPicks: number;
  verifiedPicks: number;
  metExpectation: number;
  upCount: number;
  hitRatePct?: number | null;
  winRatePct?: number | null;
  avgReturnPct?: number | null;
  avgExcessReturnPct?: number | null;
  trackedDays: number;
}

export interface PickVerificationDashboard {
  days: PickDayGroup[];
  summary: PickVerificationSummary;
}

export interface PickRunStats {
  pickDate: string;
  topN: number;
  markets: Record<string, { saved: number; skipped: number; error?: string | null }>;
  saved: number;
  skipped: number;
  errors: { market: string; message: string }[];
}

export interface PickVerifyStats {
  verifyDate: string;
  processed: number;
  done: number;
  insufficient: number;
  errors: number;
}
