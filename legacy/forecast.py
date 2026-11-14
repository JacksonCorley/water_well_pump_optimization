import pandas as pd
import numpy as np
from datetime import timedelta
from statsmodels.tsa.exponential_smoothing.ets import ETSModel
from prophet import Prophet


class forecast:
    def __init__(self, hist):
        """
        Class containing forecasting methods
        :param hist: cleaned Final_Data_hourly.csv
        """
        self.hist = hist

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

    def prof(self, start:str, end:str):
        """
        fits a prophet model to the north and south regions separately 

        output is in the form:
        df[['north_total_flow', 'south_total_flow', 'north_pred', 'south_pred']]
 
        :param start: string in format of '2016-01-01 01:00:00'
        :param end: string in format of '2016-01-02 17:00:00'
        """
        assert (pd.to_datetime(start) - timedelta(days=365)) > self.hist.index[0], f"Not enough historical data, >= 1 year is needed"
        
        # slice on all previous data to train on
        train = self.hist.loc[:pd.to_datetime(start)-timedelta(hours=1)]
        
        test = self.hist.loc[start:end].copy()
        
        # calculate elapsed time between two timestamps
        elapsed = pd.to_datetime(end) - pd.to_datetime(start)

        #calculate # of time steps in the elapsed time
        steps = (elapsed / timedelta(minutes=60)) + 1
        
        # instantiate two models 
        n = Prophet()
        s = Prophet()
        
        # slice off north and south flows
        north_train = pd.DataFrame(train['north_total_flow']).copy()
        south_train = pd.DataFrame(train['south_total_flow']).copy()
        
        # create required column name for prophet
        north_train['ds'] = north_train.index
        south_train['ds'] = south_train.index
        
        # rename the columns (this is required for prophet bc it's not flexiable as to passing col names)
        north_train.columns = ['y', 'ds']
        south_train.columns = ['y', 'ds']
        
        # rearrange col name (required)
        north_train = north_train[['ds', 'y']]
        south_train = south_train[['ds', 'y']]
        
        # fit models
        n.fit(north_train)
        s.fit(south_train)
        
        # Expand df for future preds. prophet doesnt use a separate df, it appends the date range to the training set
        # presumably this was a design decision by facebook to not have to combine tain/test sets at the end to visualize
        n_future = n.make_future_dataframe(periods=int(steps), freq='H')
        s_future = s.make_future_dataframe(periods=int(steps), freq='H')
        
        # make forecasts (this will forecast the entire dataset). there appears to be no way to limit the forecasting period
        # to just out of sample period range which is appended at the end of 
        n_fcst = n.predict(n_future)
        s_fcst = s.predict(s_future)
        
        # make the forcast index the datetime
        n_fcst.index = n_fcst['ds']
        s_fcst.index = s_fcst['ds']
        
        # combine 
        preds = self.hist[test.index[0]:test.index[-1]][['north_total_flow', 'south_total_flow']].copy()
        preds['north_pred'] = n_fcst.loc[start:end]['yhat']
        preds['south_pred'] = s_fcst.loc[start:end]['yhat']
        
        # return train/test/and forecasts
        return preds