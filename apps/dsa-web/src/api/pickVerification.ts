import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  PickRunStats,
  PickVerificationDashboard,
  PickVerifyStats,
} from '../types/pickVerification';

export const pickVerificationApi = {
  /**
   * Get pick + next-day verification dashboard data
   */
  getDashboard: async (limitDays = 30): Promise<PickVerificationDashboard> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/pick-verification/dashboard',
      { params: { limit_days: limitDays } },
    );
    return toCamelCase<PickVerificationDashboard>(response.data);
  },

  /**
   * Trigger daily screening and persist each market's Top N
   */
  runPicks: async (params: {
    markets?: string;
    top?: number;
  } = {}): Promise<PickRunStats> => {
    const queryParams: Record<string, string | number> = {};
    if (params.markets) queryParams.markets = params.markets;
    if (params.top != null) queryParams.top = params.top;

    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/pick-verification/picks',
      null,
      { params: queryParams },
    );
    return toCamelCase<PickRunStats>(response.data);
  },

  /**
   * Trigger next-day verification for persisted picks
   */
  runVerify: async (params: {
    market?: string;
    force?: boolean;
  } = {}): Promise<PickVerifyStats> => {
    const queryParams: Record<string, string | boolean> = {};
    if (params.market) queryParams.market = params.market;
    if (params.force) queryParams.force = params.force;

    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/pick-verification/verify',
      null,
      { params: queryParams },
    );
    return toCamelCase<PickVerifyStats>(response.data);
  },
};
