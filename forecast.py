from datetime import timedelta
#from statsmodels.tsa.seasonal import MSTL
from statsmodels.tsa.exponential_smoothing.ets import ETSModel
import pandas as pd

class forecast:
    def __init__(self, hist):
        """
        Class containing forecasting methods
        :param hist: cleaned Final_Data_hourly.csv
        """
        self.hist = hist

# =============================================================================
#     def naive_mstl(self, start:str, end:str):
#         """
#         Naive forecast based on multiple seasonal treand decomposition based on LOESS
#  
#         :param start: string in format of '2016-01-01 01:00:00'
#         :param end: string in format of '2016-01-02 17:00:00'
#         """
#         start = pd.to_datetime(start)
#         end = pd.to_datetime(end)
#         
#         assert (start - timedelta(days=365)) > self.hist.index[0], f"Not enough historical data, >= 1 year is needed"
#         
#         # slice on all previous data to train on
#         train = self.hist.loc[:start-timedelta(hours=1)]
# 
#         # fit models for north and south region
#         north = MSTL(train['north_total_flow'], periods=(24, 24*7)).fit(disp=0)
#         south = MSTL(train['south_total_flow'], periods=(24, 24*7)).fit(disp=0)
#         
#         # create datetime range of forecast period
#         dt_range = pd.date_range(start, end, freq='1H')
#         
#         n = []
#         s = []
# 
#         # loop throgh the range and combine the seasonal components for each region
#         for x in dt_range:
#             t = x - timedelta(days=365)
#             n.append(north.trend.loc[t] + north.seasonal.loc[t].sum())
#             s.append(south.trend.loc[t] + south.seasonal.loc[t].sum())
#         
#         # create a df for the forecast period
#         predict = self.hist.loc[start:end].copy()
#         predict['north_pred'] = n
#         predict['south_pred'] = s
#         
#         return predict[['north_total_flow', 'south_total_flow', 'north_pred', 'south_pred']]
# =============================================================================

    def ets(self, start:str, end:str):
        """
        Simple ETS model
 
        :param start: string in format of '2016-01-01 01:00:00'
        :param end: string in format of '2016-01-02 17:00:00'
        """
        assert (pd.to_datetime(start) - timedelta(days=365)) > self.hist.index[0], f"Not enough historical data, >= 1 year is needed"
        
        # slice on all previous data to train on
        train = self.hist.loc[:pd.to_datetime(start)-timedelta(hours=1)]

        # fit models for north and south region
        # ETS
        north_model = ETSModel(
            train['north_total_flow'],
            error="add",
            trend="add",
            seasonal="add",
            damped_trend=False,
            seasonal_periods=24,
        )
        north_fit = north_model.fit(disp=0)

        south_model = ETSModel(
            train['south_total_flow'],
            error="add",
            trend="add",
            seasonal="add",
            damped_trend=False,
            seasonal_periods=24,
        )
        south_fit = south_model.fit(disp=0)
        
        # create a df for the forecast period
        predict = self.hist.loc[start:end].copy()
        north_predict = north_fit.predict(start=start, end=end)
        south_predict = south_fit.predict(start=start, end=end)
        
        
        predict['north_pred'] = north_predict
        predict['south_pred'] = south_predict
        return predict[['north_total_flow', 'south_total_flow', 'north_pred', 'south_pred']]
        #return pred
