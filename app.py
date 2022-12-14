# -*- coding: utf-8 -*-
"""
Created on Wed Feb  9 14:33:38 2022

@author: JC056455
"""
import pandas as pd

import dash
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Output, Input, State
import dash_table
import dash_bootstrap_components as dbc
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import date
from datetime import timedelta
import plotly.express as px

# forecasting function
from forecast import forecast as fc

# optimization function
from optimization import optimizer

### user inputs
from plotly.validators.scatter.marker import SymbolValidator
raw_symbols = SymbolValidator().values


## constants

# %% Read in Data

## read in combined dataset and convert datetime column ot datetime
final_data = pd.read_csv("Final_Data_hourly_clean.csv")
final_data.columns = ["DateTime"]+list(final_data.columns[1:])
final_data.index = pd.to_datetime(final_data['DateTime'])
filt_data = final_data.reindex(pd.date_range(start=final_data.index[0], end=final_data.index[-1], freq='1H'))

## import datset used to summarize and perform calcs on data_df
locations_df = pd.read_csv("Well_Summarys.csv")
symbol_dict = {"PMP":200,"TNK":201,"VLV":26,"PP":35,"BPS":224}
locations_df["symbol"] = [symbol_dict[p] for p in [i.split("_")[0] for i in locations_df["Location"]]]
opt_df_lst = [locations_df[locations_df["Section"] == "South"], locations_df[locations_df["Section"] == "North"]]

#make dictionary for days slider
def make_hours_dict(hours):
    hours_dict = {}
    for i in range(hours):
        if i % (hours/24) ==0:
            hours_dict[i+1] = {'label':str(i), 'style':{'font-size':'50%'}}
        else:
            hours_dict[i+1] = {'label':"", 'style':{'font-size':'50%'}}
    return hours_dict

#%% testing vairables - need to be updated.
#testing_on_wells = ["PMP_6","PMP_1","PMP_31","PMP_15","PMP_24","PMP_15","PMP_16"]
#predicted_north = 15265
#predicted_south = 12517    
    

#%% make user interface items.
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.SLATE])
server = app.server

Title_Card = dbc.Card(
    dbc.CardBody(
        [
            dbc.Row([
                dbc.Col([html.H5("CSE 6242 Team JAARY - Well Pump Operations Prediction Dashboard", className="card-title")]),
                ]),
            ]
        )
    )

Time_Selection_Card = dbc.Card(
    dbc.CardBody(
        [
            dbc.Row([
                dbc.Col([html.P("Date Range Selection",className="card-text")]),
                dbc.Col(dcc.Store(id='table-output')),
                ]),
            dbc.Row([
                dbc.Col(dcc.DatePickerRange(
                    id='date-picker-range',
                    min_date_allowed=date(2017, 7, 9),
                    max_date_allowed=date(2019, 7, 7),
                    initial_visible_month=date(2018, 4, 14),
                    start_date=date(2018, 4, 15))),
                dbc.Col([dbc.Button(id='update_data',
                           children=[html.I(className="fa fa-download mr-1"), 'Update Pump Projections'],
                           color="info",
                           className="mt-1", ),
                         dcc.Loading(
                             id="loading-predictions",
                             type="default",
                             children=html.Div(id="loading-predictions-output"))
                         ]),
                ]),            
            dbc.Row([
                dbc.Col([html.P("Hour",className="card-text")])
                ]),
            dbc.Row([
                dcc.Slider(
                    id='hour_slider',
                    min= 1,
                    max= 48,
                    step = 1,
                    value = 1,
                    marks = make_hours_dict(48),
                    tooltip = {'always_visible': False,'placement':'bottom'},
                ),]),
            dbc.Row([
                dbc.Col([html.Div(id="cum_usage_figure")]),
                ]),
            ]
        )
    )

    
South_Pred_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("North Section: Combined Active Pumps", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Tabs(id="north_tabs", value='north_tab_1', children=[
                            dcc.Tab(label='Flow', value='north_tab_1'),
                            dcc.Tab(label='Cumulative Power Usage', value='north_tab_2'),
                            ]),
                            html.Div(id='north_forecast')])
                        ]),
                    #dbc.Row([
                    #    dcc.Graph(id='south-zone-flow-pred-graph'),
                    #    ]),
                ]
            )
        )


North_Pred_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("South Section: Combined Active Pumps", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Tabs(id="south_tabs", value='south_tab_1', children=[
                            dcc.Tab(label='Flow', value='south_tab_1'),
                            dcc.Tab(label='Cumulative Power Usage', value='south_tab_2'),
                            ]),
                            html.Div(id='south_forecast')],
                            )
                        ])
                    #dbc.Row([
                    #    dcc.Graph(id='north-zone-flow-pred-graph'),
                    #    ]),
                ]
            )
        )


well_map_Card = dbc.Card(
            dbc.CardBody(
                [
                    html.H5("Well Operations Map", className="card-title"),
                    html.Div(id='well-operations-map')
                    #dcc.Input(id="min-SCFM", type="number", value = 0, disabled = True),
                    #dcc.Input(id="max-SCFM", type="number", value = 0, disabled = True),
                ]
            )
        )


north_wells_table_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("North Well Operations Table", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.Div(id='north_datatable')]),
                        ]),
                    #dcc.Input(id="min-SCFM", type="number", value = 0, disabled = True),
                    #dcc.Input(id="max-SCFM", type="number", value = 0, disabled = True),
                ]
            )
        )

south_wells_table_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("South Well Operations Table", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.Div(id='south_datatable')]),
                        ]),
                    #dcc.Input(id="min-SCFM", type="number", value = 0, disabled = True),
                    #dcc.Input(id="max-SCFM", type="number", value = 0, disabled = True),
                ]
            )
        )

data_table_card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("Prediction Data", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.Div(id='preds-table')]),
                        ]),
                    #dcc.Input(id="min-SCFM", type="number", value = 0, disabled = True),
                    #dcc.Input(id="max-SCFM", type="number", value = 0, disabled = True),
                ]
            )
        )


cards = html.Div([
    dbc.Row([
        dbc.Col(Title_Card, width = 12),
        ]),
     dbc.Row([
         dbc.Col(Time_Selection_Card, width = 4),
         dbc.Col(South_Pred_Card, width = 4),
         dbc.Col(North_Pred_Card, width = 4),
         ]),
     dbc.Row([
         dbc.Col(well_map_Card, width = 4),
         dbc.Col(north_wells_table_Card, width = 4),
         dbc.Col(south_wells_table_Card, width = 4),
         ]),
     dbc.Row([
         dbc.Col(data_table_card, width = 12),
         ])
     ])


app.layout = html.Div(cards)

#%% Make helper functions


##optimization function.
def update_optimization(df, predicted_north, predicted_south):
    optimal_pumps = []
    for itr, i_df in enumerate(opt_df_lst):
        if itr == 0:
            optimal_pumps = optimal_pumps + optimizer(i_df, thresh = predicted_south)
            opt_filt_df = i_df.loc[[row for row in i_df.index if i_df.at[row,"Location"] in optimal_pumps]]
            south_q = opt_filt_df["Average_Flow(gpm)"].sum()
            south_usage = opt_filt_df["Average_Power_Usage(kW-Hr)"].sum()
        else:
            optimal_pumps = optimal_pumps + optimizer(i_df, thresh = predicted_north)
            opt_filt_df = i_df.loc[[row for row in i_df.index if i_df.at[row,"Location"] in optimal_pumps]]
            north_q = opt_filt_df["Average_Flow(gpm)"].sum()
            north_usage = opt_filt_df["Average_Power_Usage(kW-Hr)"].sum()
    return optimal_pumps, north_q, south_q, north_usage, south_usage

##sums power usage based on the optimized pumps.
def get_cum_usage(prediction_df):
    north_ts_usage = []
    north_cum_usage =0
    south_ts_usage = []
    south_cum_usage =0
    for row in list(prediction_df.index):
        optimal_pumps, north_q, south_q, north_usage, south_usage = update_optimization(locations_df, prediction_df.at[row,"north_pred"], prediction_df.at[row,"south_pred"])
        north_cum_usage = north_cum_usage + north_usage
        north_ts_usage.append(north_cum_usage)
        south_cum_usage = south_cum_usage + south_usage
        south_ts_usage.append(south_cum_usage)
    return north_cum_usage,  south_cum_usage, north_ts_usage, south_ts_usage

#function to update geographics plot of online pumps
def generate_geo_plot(Dataframe, on_predictions):
    on_wls=[]
    for wll in Dataframe["Location"]:
        if wll in on_predictions:
            on_wls.append(12)
        else:
            on_wls.append(6)
            
    Dataframe["Online"] = on_wls
    North_df = Dataframe[Dataframe["Section"]=="North"]
    South_df = Dataframe[Dataframe["Section"]=="South"]
    
    fig = go.Figure(go.Scatter(mode="markers",
                               x=list(North_df["X"]),
                               y=list(North_df["Y"]),
                               name = "North Pumps",
                               hovertemplate =
                               '<b>Pump</b>: %{text}',
                               text = [pmp for pmp in North_df["Location"]],
                               marker_symbol=list(North_df["symbol"]),
                               marker_line_color="midnightblue",
                               marker_color="firebrick",
                               marker_line_width=list([round(i/8) for i in North_df["Online"]]),
                               marker_size=list(North_df["Online"]))) 
                 
    fig.add_trace(go.Scatter(mode="markers",
                             x=list(South_df["X"]),
                             y=list(South_df["Y"]),
                             name = "South Pumps",
                             hovertemplate =
                             '<b>Pump</b>: %{text}',
                             text = [pmp for pmp in South_df["Location"]],
                             marker_symbol=list(South_df["symbol"]),
                             marker_line_color="midnightblue",
                             marker_color="lightskyblue",
                             marker_line_width=list([round(i/8) for i in South_df["Online"]]),
                             marker_size=list(South_df["Online"])))
             
    fig.update_layout(legend=dict(
        yanchor="top",
        y=0.99,
        xanchor="left",
        x=0.01),
        xaxis=dict(showgrid=False, zeroline=False, visible= False),  # numbers below),
        yaxis=dict(showgrid=False, zeroline=False, visible= False),
        margin=dict(l=2, r=2, t=30, b=2),
        plot_bgcolor="#e3fae5",
        height=225,
        #paper_bgcolor="#d0f7d2"
        )
    
    return fig

#function to update South and North tables based on a list output from from pump well predictiosn function
def update_table(Dataframe, on_predictions):
    on_wls=[]
    for wll in Dataframe["Location"]:
        if wll in on_predictions:
            on_wls.append(1)
        else:
            on_wls.append(0)
            
    Dataframe["Online"] = on_wls
    Dataframe.columns = ['Location', 'Section', 'X', 'Y', 'Group', 'Static (ft)','Specfic Capacity (gpm/ft)', 'Average_Flow(gpm)', 'Average_Press(psi)','Average_Power_Usage(kW-Hr)', 'symbol', 'Online']
    North_df = Dataframe[(Dataframe["Section"]=="North") & (Dataframe["Online"]==1)]
    South_df = Dataframe[(Dataframe["Section"]=="South") & (Dataframe["Online"]==1)]
    desirable_cols = ["Location","Average_Flow(gpm)","Average_Power_Usage(kW-Hr)"]
    
    return North_df[desirable_cols], South_df[desirable_cols]
    

#function to update South and North forecasts and plots
def plot_flow_forecast(dataframe, hour_inp, section):
    ##UPDATE WITH ACTUAL FORECAST DATA
    section = section.lower()
    plot_df = dataframe.copy()
    # =============================================================================
    # power_cum = []
    # powersum = 0
    # for i, value in enumerate(plot_df[section+"_total_power_usage"]):
    #     powersum = powersum + value
    #     power_cum.append(powersum)
    #     if plot_df.at[i,"level_0"] == 0:
    #         start_power_sum = powersum
    # plot_df = plot_df.drop(columns=[section+"_total_power_usage"])
    # plot_df[section+"_power_usage"] = power_cum
    # =============================================================================

    start_index = plot_df.index[0]
    #power_x = start_index + timedelta(hour_inp)
    #power_y = plot_df.at[start_index + hours,section+"_pred"]
    flow_x = start_index + timedelta(hours = hour_inp)
    flow_pred = plot_df.at[start_index + timedelta(hours = hour_inp),section+"_pred"]
    flow_hist = plot_df.at[start_index + timedelta(hours = hour_inp),section+"_total_flow"]
    fig = make_subplots(specs=[[{"secondary_y": False}]])
    section_colors = {"north":"firebrick","south":"lightskyblue"}
    trend_colors = {0:"black",1:"blue",2:"orange",3:"gray"}
    trend_opac = {"historical":0.5,"forecast":1}
    y_1_max=0
    for i, var in enumerate([i for i in plot_df.columns if section in i]):
        if "pred" in var:
            trend_type = "forecast"
            flow_y = flow_pred
        else:
            trend_type = "historical"
            flow_y = flow_hist

        secondary_bool = False
        y_1_max = max(plot_df[var].max()*1.1,y_1_max)
        color = section_colors[section]
        fig.add_trace(go.Scatter(mode="lines",
                                 x=list(plot_df.index),
                                 y=list(plot_df[var]),
                                 name = trend_type,
                                 hovertemplate =
                                 '<b>Date Time</b>: %{x}' +
                                 '<br><b>Value</b>: %{y:.2f}' +
                                 '<br><b>Trend Type</b>: %{text}',
                                 text = [trend_type]*len(plot_df),
                                 marker_line_color=color,
                                 marker_color=color,
                                 opacity = trend_opac[trend_type]),secondary_y = secondary_bool)
        fig.add_trace(go.Scatter(mode="markers",
                                 name = trend_type,
                                  x=[flow_x],
                                  y=[flow_y],
                                  marker_line_color="black",
                                  marker_color=color,
                                  marker_size = 6,
                                  marker_line_width=1,
                                  showlegend = False,
                                  opacity = trend_opac[trend_type]))
    fig.update_layout(showlegend=True,
        #yaxis2=dict(range=[0,y_2_max], showgrid=False, zeroline=False, visible = False),
        xaxis=dict(range = [plot_df.index[0],plot_df.index[-1]], showgrid=False, zeroline=False, visible = True),  # numbers below),
        yaxis=dict(range = [0,y_1_max], showgrid=False, zeroline=False, visible = True),
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title = "Flow (gpm)",
        height = 220,
        #yaxis_title="Demand (gpm)",
        #yaxis2_title="Power Usage (kw-hr)",
        plot_bgcolor="#fafbfc"
        )
    return fig, flow_pred

def plot_usage_forecast(pred_dataframe, online_wells, north_for_usage, south_for_usage, hour_inp, section):
    ##UPDATE WITH ACTUAL FORECAST DATA
    section = section.lower()
    plot_df = pred_dataframe.copy()
    hist_data_df = filt_data.loc[plot_df.index[0]:plot_df.index[-1]]
    south_cum_usage = 0
    south_hist_usg = []
    north_cum_usage = 0
    north_hist_usg = []
    for row in list(hist_data_df.index):
        south_cum_usage = south_cum_usage + hist_data_df.at[row,"south_total_power_usage"]
        south_hist_usg.append(south_cum_usage)
        north_cum_usage = north_cum_usage + hist_data_df.at[row,"north_total_power_usage"]
        north_hist_usg.append(north_cum_usage)
    plot_df["north_historical_usage"] = north_hist_usg
    plot_df["south_historical_usage"] = south_hist_usg
    plot_df["north_optimized_usage"] = north_for_usage
    plot_df["south_optimized_usage"] = south_for_usage
    if section.lower() == "south":
        return_usage = south_cum_usage
    else:
        return_usage = north_cum_usage
    #print("plot_df = ", plot_df)
    # =============================================================================
    # power_cum = []
    # powersum = 0
    # for i, value in enumerate(plot_df[section+"_total_power_usage"]):
    #     powersum = powersum + value
    #     power_cum.append(powersum)
    #     if plot_df.at[i,"level_0"] == 0:
    #         start_power_sum = powersum
    # plot_df = plot_df.drop(columns=[section+"_total_power_usage"])
    # plot_df[section+"_power_usage"] = power_cum
    # =============================================================================

    start_index = plot_df.index[0]
    #power_x = start_index + timedelta(hour_inp)
    #power_y = plot_df.at[start_index + hours,section+"_pred"]
    flow_x = start_index + timedelta(hours = hour_inp)
    flow_pred = plot_df.at[start_index + timedelta(hours = hour_inp),section+"_optimized_usage"]
    flow_hist = plot_df.at[start_index + timedelta(hours = hour_inp),section+"_historical_usage"]
    fig = make_subplots(specs=[[{"secondary_y": False}]])
    section_colors = {"north":"firebrick","south":"lightskyblue"}
    trend_colors = {0:"black",1:"blue",2:"orange",3:"gray"}
    trend_opac = {"historical":0.5,"optimized":1}
    y_1_max=0
    for i, var in enumerate([i for i in plot_df.columns if (section in i) & ("usage" in i)]):
        if "optimized" in var:
            trend_type = "optimized"
            flow_y = flow_pred
        else:
            trend_type = "historical"
            flow_y = flow_hist

        secondary_bool = False
        y_1_max = max(plot_df[var].max()*1.1,y_1_max)
        color = section_colors[section]
        fig.add_trace(go.Scatter(mode="lines",
                                 x=list(plot_df.index),
                                 y=list(plot_df[var]),
                                 name = trend_type,
                                 hovertemplate =
                                 '<b>Date Time</b>: %{x}' +
                                 '<br><b>Value</b>: %{y:.2f}' +
                                 '<br><b>Trend Type</b>: %{text}',
                                 text = [trend_type]*len(plot_df),
                                 marker_line_color=color,
                                 marker_color=color,
                                 opacity = trend_opac[trend_type]),secondary_y = secondary_bool)
        fig.add_trace(go.Scatter(mode="markers",
                                  name = trend_type,
                                  x=[flow_x],
                                  y=[flow_y],
                                  marker_line_color="black",
                                  marker_color=color,
                                  marker_size = 6,
                                  marker_line_width=1,
                                  showlegend = False,
                                  opacity = trend_opac[trend_type]))
    fig.update_layout(showlegend=True,
        #yaxis2=dict(range=[0,y_2_max], showgrid=False, zeroline=False, visible = False),
        xaxis=dict(range = [plot_df.index[0],plot_df.index[-1]], showgrid=False, zeroline=False, visible = True),  # numbers below),
        yaxis=dict(range = [0,y_1_max], showgrid=False, zeroline=False, visible = True),
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis_title = "Power Usage (kW-Hr)",
        height = 220,
        #yaxis_title="Demand (gpm)",
        #yaxis2_title="Power Usage (kw-hr)",
        plot_bgcolor="#fafbfc"
        )
    return fig, return_usage, plot_df

def convert_stored_dict_to_df(data_dictionary):
    dat_dict = {}
    for row in data_dictionary:
        #print(row)
        for key in row.keys():
            if key in dat_dict.keys():
                dat_dict[key].append(row[key])
            else:
                dat_dict[key] = [row[key]]
    #print("dat_dict = ", dat_dict)
    data_df = pd.DataFrame(dat_dict)
    data_df.index = pd.to_datetime(data_df['index'])
    data_df = data_df.drop(columns='index')
    return data_df


#%% Dash Callback functions. 
# callback to run the predictions on the timeseries data. Run only when the update predictions is clicked.
# =============================================================================
@app.callback([Output('table-output', 'data'),
               Output('update_data', 'children'),
               Output('update_data', 'color'),
               Output("loading-predictions-output", "children")],
              [Input('update_data','n_clicks'),
               State('date-picker-range','start_date'),
               State('date-picker-range','end_date')],
               prevent_initial_call=True)
def update_predictions(clicks, start_date, end_date):
    if (start_date is None) | (end_date is None):
        # Return all the rows on initial load/no country selected.
        raise PreventUpdate
    else:
        m = fc(filt_data)
         
        pred = m.prof(start=start_date, end=end_date)
        pred = pred.reset_index()
    return pred.to_dict('records'), [html.I(className="fa fa-download mr-1"), 'Update Pump Projections'] , "info", "Predictions Updated"
# 
# #callback to update the map, north and south tables, forecast graphs, and optimization information. This is run when hour is changed or the update predictions is clicked.
@app.callback([Output('well-operations-map','children'),
              Output('north_datatable', 'children'),
              Output('south_datatable', 'children'),
              Output('preds-table', 'children'),
              Output('north_forecast', 'children'),
              Output('south_forecast', 'children'),
              Output('cum_usage_figure', 'children')],
              [Input('table-output', 'data'),
               Input('hour_slider', 'value'),
               Input('north_tabs', 'value'),
               Input('south_tabs', 'value')],
              prevent_initial_call=True)
def on_data_set_table(data, hour_inp, north_tab_val, south_tab_val):
    if data is None:
        raise PreventUpdate
        
    #print(data)
    #data is stored in memory and brought in from dcc.Store component. Needs to be converted to df.
    data_df = convert_stored_dict_to_df(data)
    data_df = data_df.dropna()
    #make table output for predictions
    mod_data_df=data_df.reset_index()
    mod_data_df.columns = ["DateTime"]+list(mod_data_df.columns[1:])
    
    #determien the cumulative north and south power usage.
    north_cum_usage,  south_cum_usage, north_ts_usage, south_ts_usage = get_cum_usage(mod_data_df)
    
    #get the optimized online wells. other vairables aren'r currently being displayed.
    north_pred = data_df.at[data_df.index[0] + timedelta(hours = hour_inp),"north_pred"]
    south_pred = data_df.at[data_df.index[0] + timedelta(hours = hour_inp),"south_pred"]
    online_wells, north_q, south_q, north_usage, south_usage = update_optimization(locations_df, north_pred, south_pred)
    
    #update forecast plots and get current forecast flow value for the north.
    north_flow_fig, north_pred = plot_flow_forecast(data_df,hour_inp,"north")
    north_usage_fig, north_hist_usage, _ = plot_usage_forecast(data_df, online_wells, north_ts_usage, south_ts_usage,hour_inp,"north")
    south_flow_fig, south_pred = plot_flow_forecast(data_df,hour_inp,"south")
    south_usage_fig, south_hist_usage, new_plot_df = plot_usage_forecast(data_df, online_wells, north_ts_usage, south_ts_usage, hour_inp,"south")
    new_mod_data_df=new_plot_df.reset_index()
    round_cols = ["Hist North Flow","Hist South Flow","Pred North Flow","Pred South Flow","Hist North Usage","Hist South Usage","Opt North Usage","Opt South Usage"]
    new_mod_data_df.columns = ["DateTime"] + round_cols
    new_mod_data_df[round_cols] = new_mod_data_df[round_cols].round(1)
    
    if north_tab_val == "north_tab_1":
        north_fig_ret = dcc.Graph(id="north_flow_figure",figure = north_flow_fig)
    elif north_tab_val == "north_tab_2":
        north_fig_ret = dcc.Graph(id="north_usage_figure",figure = north_usage_fig)
    #update forecast plots and get current forecast flow value for the south.
    if south_tab_val == "south_tab_1":
        south_fig_ret = dcc.Graph(id="south_flow_figure",figure = south_flow_fig)
    elif south_tab_val == "south_tab_2":
        south_fig_ret = dcc.Graph(id="south_usage_figure",figure = south_usage_fig)

    #update the geopraphic map of pumps
    ops_map_fig = generate_geo_plot(locations_df,online_wells)
    ops_map_fig_dcc_graph = dcc.Graph(id="well_ops_graph",figure = ops_map_fig)
    #get the dataframes for the north and south online pumps.
    north_data, south_data = update_table(locations_df,online_wells)
    section_cols = ["Pump","Average Flow (gpm)","Average Power Usage (kW-Hr)"]
    north_data.columns = section_cols
    south_data.columns = section_cols
    
    #make table output for predictions
    preds_df = [html.Div([
             dash_table.DataTable(id = "predicted_values",
                 data=new_mod_data_df.to_dict('records'),
                 columns=[{'name': i, 'id': i} for i in new_mod_data_df.columns],
                 style_data={
                     'color': 'black',
                     'backgroundColor': '#edeff0'
                     },
                 style_data_conditional=[
                     {
                         'if': {'row_index': 'odd'},
                         'backgroundColor': '#c4c2c3',
                         }],
                 style_header={
                     'backgroundColor': '#c4c2c3',
                     'color': 'Black',
                     'fontWeight': 'bold',
                     'border': '1px solid black'
                     },
                 editable=False)])]
    
    # make table for north optimized pumps
    north_df = [html.Div([
             dash_table.DataTable(id = "north_on_pumps",
                 data=north_data.to_dict('records'),
                 columns=[{'name': i, 'id': i} for i in north_data.columns],
                 style_data={
                     'color': 'black',
                     'backgroundColor': '#e3c3c6',
                     'border': '1px solid black'
                     },
                 style_data_conditional=[
                     {
                         'if': {'row_index': 'odd'},
                         'backgroundColor': 'white',
                         }],
                 style_header={
                     'backgroundColor': 'white',
                     'color': 'black',
                     'border': '1px solid black'
                     #'fontWeight': 'bold'
                     },
                 editable=False)])]
    
    # make table for south optimized pumps 
    south_df = [html.Div([
             dash_table.DataTable(id = "south_on_pump",
                 data=south_data.to_dict('records'),
                 columns=[{'name': i, 'id': i} for i in south_data.columns],
                 style_data={
                     'color': 'black',
                     'backgroundColor': '#cee8ed',
                     'border': '1px solid black'
                     },
                 style_data_conditional=[
                     {
                         'if': {'row_index': 'odd'},
                         'backgroundColor': 'white',
                         }],
                 style_header={
                     'backgroundColor': 'white',
                     'color': 'black',
                     'border': '1px solid black'
                     #'fontWeight': 'bold'
                     },
                 editable=False)])]
    
    cum_usage_df = pd.DataFrame(data = {"Trend":["historical","optimized","historical","optimized"],"Power Usage":[round(north_hist_usage,1),round(north_cum_usage,1),round(south_hist_usage,1),round(south_cum_usage,1)],"Section":["North","North","South","South"]})

    cum_usage_fig = px.bar(cum_usage_df, x="Power Usage", y="Trend", color='Section', orientation='h',
                 hover_data=["Trend", "Power Usage"],
                 height=150,
                 color_discrete_sequence=[
                     "firebrick", "lightskyblue"],
                 title='Cumulative Power Usage (kW-Hr)')

    cum_usage_fig.update_layout(showlegend=True,yaxis=dict(showgrid=False, visible = True),
                                yaxis_title = "",
                                xaxis_title = "",
                                margin=dict(l=10, r=10, t=50, b=5))
    
    cum_usage_fig_graph = dcc.Graph(id = "cum_usage_bar_chart", figure = cum_usage_fig)
    
    return ops_map_fig_dcc_graph, north_df, south_df, preds_df, north_fig_ret, south_fig_ret, cum_usage_fig_graph

# =============================================================================

#updates number of days in month based on selection from month.
@app.callback([Output(component_id='hour_slider', component_property='max'),
               Output(component_id='hour_slider', component_property='marks')],
              Input(component_id='date-picker-range', component_property='end_date'),
              Input(component_id='date-picker-range', component_property='start_date'),
              prevent_initial_call=False)
def update_input_dates_callback(end_date, start_date):
    if (start_date is not None) & (end_date is not None):
        days_diff = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days
#not currently used. Used to be used to make 48 hrous the maximum vieable interval for the date range.
# =============================================================================
#         if days_diff > 2:
#             #add a prompt here to warn the user 48 horus in teh maximum.
#             new_end_date = pd.to_datetime(start_date) + timedelta(hours=48)
#         else:
#             new_end_date = end_date
#         if (pd.to_datetime(new_end_date) - pd.to_datetime(start_date)).total_seconds() / 3600 > 48:
#             new_end_date = pd.to_datetime(start_date) + timedelta(hours=48)
#         days_diff = (pd.to_datetime(new_end_date) - pd.to_datetime(start_date)).days
# =============================================================================
        hours = days_diff*24
        marks = make_hours_dict(hours)
        return hours, marks
    
if __name__ == '__main__':
    app.run_server(debug=False) 
