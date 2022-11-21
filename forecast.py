from datetime import timedelta
from statsmodels.tsa.seasonal import MSTL
import pandas as pd

class forecast:
    def __init__(self, hist):
        """
        Class containing forecasting methods
        :param hist: cleaned Final_Data_hourly.csv
        """
        self.hist = hist

    def naive_mstl(self, start:str, end:str):
        """
        Naive forecast based on multiple seasonal treand decomposition based on LOESS
 
        :param start: string in format of '2016-01-01 01:00:00'
        :param end: string in format of '2016-01-02 17:00:00'
        """
        start = pd.to_datetime(start)
        end = pd.to_datetime(end)
        
        assert (start - timedelta(days=365)) > self.hist.index[0], f"Not enough historical data, >= 1 year is needed"
        
        # slice on all previous data to train on
        train = self.hist.loc[:start-timedelta(hours=1)]

        # fit models for north and south region
        north = MSTL(train['north_total_flow'], periods=(24, 24*7)).fit()
        south = MSTL(train['south_total_flow'], periods=(24, 24*7)).fit()
        
        # create datetime range of forecast period
        dt_range = pd.date_range(start, end, freq='1H')
        
        n = []
        s = []

        # loop throgh the range and combine the seasonal components for each region
        for x in dt_range:
            t = x - timedelta(days=365)
            n.append(north.trend.loc[t] + north.seasonal.loc[t].sum())
            s.append(south.trend.loc[t] + south.seasonal.loc[t].sum())
        
        # create a df for the forecast period
        predict = self.hist.loc[start:end].copy()
        predict['north_pred'] = n
        predict['south_pred'] = s
        
        return predict[['north_total_flow', 'south_total_flow', 'north_pred', 'south_pred']]