import apiClient from './index';
import { toCamelCase } from './utils';
import type { PaperDashboard } from '../types/paper';

export const paperApi = {
  /**
   * Get paper account dashboard (equity curve, positions, trades, risk config)
   */
  getDashboard: async (
    curveDays = 30,
    tradeLimit = 30,
  ): Promise<PaperDashboard> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/paper/dashboard',
      { params: { curve_days: curveDays, trade_limit: tradeLimit } },
    );
    return toCamelCase<PaperDashboard>(response.data);
  },
};
