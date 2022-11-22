# -*- coding: utf-8 -*-
"""
Created on Wed Feb  9 14:33:38 2022

@author: JC056455
"""
import base64
import datetime
import io
import math
import pandas as pd
import os

from plotly.offline import plot
import dash
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Output, Input, State
import dash_table
import dash_bootstrap_components as dbc
#import pandas as pd
import plotly.express as px
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
#import shapely
#from shapely.geometry import LineString, Point

# forecasting function
#from forecast.forecast import naive_mstl
from forecast import forecast 


### user inputs
from plotly.validators.scatter.marker import SymbolValidator
raw_symbols = SymbolValidator().values

#os.chdir(r"C:\Users\jdcor\OneDrive\Documents\GitHub\CSE6242\app")

## constants

# %% Read in Data

## read in combined dataset and convert datetime column ot datetime
data_df = pd.read_csv("Final_Data_hourly.csv")
data_df['DateTime'] = pd.to_datetime(data_df['DateTime'])


## import datset used to summarize and perform calcs on data_df
locations_df = pd.read_csv("Well_Summarys.csv")
symbol_dict = {"PMP":200,"TNK":201,"VLV":26,"PP":35,"BPS":224}
locations_df["symbol"] = [symbol_dict[p] for p in [i.split("_")[0] for i in locations_df["Location"]]]
init_df = pd.read_csv("Well_Summarys.csv")

#make dictionary for days in a month and input to slider
months_dict = {}
month_key = {1:["Jan",31],2:["Feb",29],3:["Mar",31],4:["Apr",30],5:["May",31],6:["Jun",30],7:["Jul",31],8:["Aug",31],9:["Sep",30],10:["Oct",31],11:["Nov",30],12:["Dec",31]}
for i in range(12):
    months_dict[i+1] = {'label':month_key[i+1][0], 'style':{'font-size':'70%'}}

#make dictionary for days slider
days_dict = {}
for i in range(month_key[1][1]):
    days_dict[i+1] = {'label':str(i+1), 'style':{'font-size':'70%'}}

#make dictionary for days slider
hours_dict = {}
for i in range(48):
    hours_dict[i+1] = {'label':str(i+1), 'style':{'font-size':'70%'}}

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
                dbc.Col([html.H5("CSE 6242 Team JAARY", className="card-title")]),
                dbc.Col([html.P("Well Pump Operations Prediction Dashboard",className="card-text")]),
                ]),
            ]
        )
    )

Time_Selection_Card = dbc.Card(
    dbc.CardBody(
        [
            dbc.Row([
                dbc.Col([html.H5("Time Inputs", className="card-title")]),
                dbc.Col(html.Button('Update Pump Projections', id='update_data', n_clicks=0)),
                ]),
            dbc.Row([
                dbc.Col([html.P("Month",className="card-text")])
                ]),
            dbc.Row([
                dcc.Slider(
                    id='month_slider',
                    min= 1,
                    max= 12,
                    step = 1,
                    value = 1,
                    marks = months_dict,
                    tooltip = {'always_visible': False,'placement':'bottom'},
                ),]),
            dbc.Row([
                dbc.Col([html.P("Day",className="card-text")])
                ]),
            dbc.Row([
                dcc.Slider(
                    id='day_slider',
                    min= 1,
                    max= 31,
                    step = 1,
                    value = 1,
                    marks = days_dict,
                    tooltip = {'always_visible': False,'placement':'bottom'},
                ),]),            
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
                    marks = hours_dict,
                    tooltip = {'always_visible': False,'placement':'bottom'},
                ),]),             
            ]
        )
    )

    
South_Pred_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("North Section", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.P("Flow Predictions (gpm)",className="card-text")]),
                        dbc.Col([html.P("48 - Hour Power Usage (kW-hr)",className="card-text")]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Input(id="north-flow-pred", type="number", value = 0, disabled = True, style={'width':'60%'})]),
                        dbc.Col([dcc.Input(id="north-power-pred", type="number", value = 0, disabled =True, style={'width':'60%'})]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Graph(id='north_forecast')]),
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
                        dbc.Col([html.H5("South Section", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.P("Flow Predictions (gpm)",className="card-text")]),
                        dbc.Col([html.P("48 - Hour Power Usage (kW-hr)",className="card-text")]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Input(id="south-flow-pred", type="number", value = 0, disabled =True, style={'width':'60%'})]),
                        dbc.Col([dcc.Input(id="south-power-pred", type="number", value = 0, disabled =True, style={'width':'60%'})]),
                        ]),
                    dbc.Row([
                        dbc.Col([dcc.Graph(id='south_forecast')]),
                        ]),
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
                    dcc.Graph(id='well-operations-map')
                    #dcc.Input(id="min-SCFM", type="number", value = 0, disabled = True),
                    #dcc.Input(id="max-SCFM", type="number", value = 0, disabled = True),
                ]
            )
        )


well_table_Card = dbc.Card(
            dbc.CardBody(
                [
                    dbc.Row([
                        dbc.Col([html.H5("Well Operations Table", className="card-title")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.P("North Section", className="card-text")]),
                        dbc.Col([html.P("South Section", className="card-text")]),
                        ]),
                    dbc.Row([
                        dbc.Col([html.Div(id='north_datatable')]),
                        dbc.Col([html.Div(id='south_datatable')]),
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
         dbc.Col(Time_Selection_Card, width = 6),
         dbc.Col(South_Pred_Card, width = 3),
         dbc.Col(North_Pred_Card, width = 3),
         ]),
     dbc.Row([
         dbc.Col(well_map_Card, width = 6),
         dbc.Col(well_table_Card, width = 6),
         ])
     ])


app.layout = html.Div(cards)

#%% Make helper functions


##place holder for optimization
def update_optimization(df, predicted_north,predicted_south, hour):
    ###UPDATE WITH ACTUAL OPTIMIZATION
    north_pmps = []
    south_pmps =[]
    north_q = 0
    north_usage = 0
    south_q = 0
    south_usage = 0
    div = hour % 2 + 1
    for r, section in enumerate(list(set(df["Section"]))):
        i_data = df[df["Section"] == section]
        for i, ind in enumerate(list(i_data["Location"].index)):
            if section.lower() == "north":
                if i % div == 0:
                    if i_data.at[ind,"Average_Flow(gpm)"] > 0:
                        north_q += i_data.at[ind,"Average_Flow(gpm)"]
                        north_usage += i_data.at[ind,"Average_Power_Usage(kW-Hr)"]
                        north_pmps.append(i_data.at[ind,"Location"])
                        if (north_q > predicted_north*0.85) & (north_q < predicted_north*1.15):
                            break
            elif section.lower() == "south":
                if i % div == 0:
                    if i_data.at[ind,"Average_Flow(gpm)"] > 0:
                        south_q += i_data.at[ind,"Average_Flow(gpm)"]
                        south_usage += i_data.at[ind,"Average_Power_Usage(kW-Hr)"]
                        south_pmps.append(i_data.at[ind,"Location"])
                        if (south_q > predicted_south*0.85) & (south_q < predicted_south*1.15):
                            break
    return north_pmps + south_pmps, north_q, south_q, north_usage, south_usage

## updates number of days in month based on month selection
def update_days(month):
    days_dict = {}
    for i in range(month):
        days_dict[i+1] = {'label':str(i+1), 'style':{'font-size':'70%'}}
    return days_dict

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
                               marker_color="red",
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
        margin=dict(l=10, r=10, t=30, b=10),
        plot_bgcolor="#e3fae5"
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
    Dataframe.columns = ['Location', 'Section', 'X', 'Y', 'Group', 'Static (ft)','Specfic Capacity (gpm/ft)', 'Flow(gpm)', 'Average_Press(psi)','Power_Usage(kW-Hr)', 'symbol', 'Online']
    North_df = Dataframe[(Dataframe["Section"]=="North") & (Dataframe["Online"]==1)]
    South_df = Dataframe[(Dataframe["Section"]=="South") & (Dataframe["Online"]==1)]
    desirable_cols = ["Location","Flow(gpm)","Power_Usage(kW-Hr)"]
    
    return North_df[desirable_cols], South_df[desirable_cols]
    

#function to update South and North forecasts and plots
def plot_forecast(dataframe, days_before, month, day, hour_inp, section):
    ##UPDATE WITH ACTUAL FORECAST DATA
    strt_dataframe = dataframe[(dataframe["month"] == month) & (dataframe["hour"] == 0) & (dataframe["day"] == day)].copy()
    start_index = strt_dataframe.index.max() - (days_before * 24)
    end_index = strt_dataframe.index.max() + (48)
    section = section.lower()
    hist_df = dataframe[start_index:strt_dataframe.index.max()][[section+"_total_flow",section+"_total_power_usage"]].reset_index().reset_index()[["level_0", section+"_total_flow",section+"_total_power_usage"]]
    hist_df["trend_type"] = "historical"
    forecast_df = dataframe[strt_dataframe.index.max():end_index][[section+"_total_flow",section+"_total_power_usage"]].reset_index().reset_index()[["level_0", section+"_total_flow",section+"_total_power_usage"]]
    forecast_df["trend_type"] = "forecast"
    plot_df = hist_df.append(forecast_df)
    plot_df=plot_df.reset_index()
    power_cum = []
    powersum = 0
    for i, value in enumerate(plot_df[section+"_total_power_usage"]):
        powersum = powersum + value
        power_cum.append(powersum)
        if plot_df.at[i,"level_0"] == 0:
            start_power_sum = powersum
    plot_df = plot_df.drop(columns=[section+"_total_power_usage"])
    plot_df[section+"_power_usage"] = power_cum
    plot_df = plot_df.drop(columns=["index"])
    plot_df_unpivoted = plot_df.melt(id_vars=['level_0',"trend_type"], var_name='Trend', value_name="Value")
    pu_forecast_start_index = plot_df_unpivoted[(plot_df_unpivoted["level_0"]==0) & (plot_df_unpivoted["trend_type"]=="forecast")& (plot_df_unpivoted["Trend"]==section+"_power_usage")].index[0]
    flow_forecast_start_index = plot_df_unpivoted[(plot_df_unpivoted["level_0"]==0) & (plot_df_unpivoted["trend_type"]=="forecast")& (plot_df_unpivoted["Trend"]==section+"_total_flow")].index[0]
    power_x = flow_forecast_start_index + hour_inp - 2
    power_y = plot_df_unpivoted.at[pu_forecast_start_index + hour_inp - 1,"Value"]
    flow_x = flow_forecast_start_index + hour_inp - 2
    flow_y = plot_df_unpivoted.at[flow_forecast_start_index + hour_inp - 1,"Value"]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    section_colors = {"north":"red","south":"lightskyblue"}
    trend_colors = {0:"black",1:"blue",2:"orange",3:"gray"}
    trend_opac = {"historical":0.5,"forecast":1}
    for i, var in enumerate(list(set(plot_df_unpivoted["Trend"]))):
        i_df = plot_df_unpivoted[plot_df_unpivoted["Trend"] == var]
        last_index = 0
        if "power_usage" in var:
            secondary_bool = True
            y_2_max = i_df["Value"].max()*1.7
        else:
            secondary_bool = False
            y_1_max = i_df["Value"].max()*1.1
        for trend_type in ["historical","forecast"]:
            i_plot_df = i_df[i_df["trend_type"] == trend_type]
            if "power_usage" in var:
                color = trend_colors[0]
            else:
                color = section_colors[section]
            fig.add_trace(go.Scatter(mode="lines",
                                     x=list([row + last_index for row in i_plot_df["level_0"]]),
                                     y=list(i_plot_df["Value"]),
                                     name = var + "-" + trend_type,
                                     hovertemplate =
                                     '<b>Hour</b>: %{x}' +
                                     '<br><b>Value</b>: %{y:.2f}' +
                                     '<br><b>Trend Type</b>: %{text}',
                                     text = [row for row in i_plot_df["trend_type"]],
                                     marker_line_color=color,
                                     marker_color=color,
                                     opacity = trend_opac[trend_type]),secondary_y = secondary_bool)
                                     #marker_line_width=list([round(i/8) for i in South_df["Online"]]),
                                     #marker_size=list(South_df["Online"])))
            last_index = i_plot_df["level_0"].max()
            
    fig.add_trace(go.Scatter(mode="markers",
                             x=[power_x],
                             y=[power_y],
                             marker_line_color="black",
                             marker_color=trend_colors[0],
                             marker_size = 6,
                             marker_line_width=1),secondary_y = True)
    fig.add_trace(go.Scatter(mode="markers",
                             x=[flow_x],
                             y=[flow_y],
                             marker_line_color="black",
                             marker_color=section_colors[section],
                             marker_size = 6,
                             marker_line_width=1))
                             #marker_line_width=list([round(i/8) for i in South_df["Online"]]),
                             #marker_size=list(South_df["Online"]))))
    fig.update_layout(showlegend=False,
        yaxis2=dict(range=[0,y_2_max], showgrid=False, zeroline=False, visible = False),
        xaxis=dict(showgrid=False, zeroline=False, visible = False),  # numbers below),
        yaxis=dict(range = [0,y_1_max], showgrid=False, zeroline=False, visible = False),
        margin=dict(l=10, r=10, t=30, b=10),
        height=150,
        xaxis_title="Hours",
        #yaxis_title="Demand (gpm)",
        #yaxis2_title="Power Usage (kw-hr)",
        plot_bgcolor="#fafbfc"
        )
    #fig.write_html(section+"_"+str(month)+"_"+str(day)+"_"+str(hour)+"_forecast_plot.html")
    if hour_inp == 0:
        forecast_power_sum = powersum - start_power_sum
    else:
        forecast_power_sum = power_y - start_power_sum
    return fig, forecast_power_sum, flow_y

#plot(fig)

## Define interactions with the User interface.

## main callback that is run whenever update_data in lciskced or hour slider is changed.
@app.callback([Output(component_id='well-operations-map', component_property='figure'),
              Output(component_id='north_datatable', component_property='children'),
              Output(component_id='south_datatable', component_property='children'),
              Output(component_id='north-flow-pred', component_property='value'),
              Output(component_id='south-flow-pred', component_property='value'),
              Output(component_id='north_forecast', component_property='figure'),
              Output(component_id='north-power-pred', component_property='value'),
              Output(component_id='south_forecast', component_property='figure'),
              Output(component_id='south-power-pred', component_property='value'),],
              [Input('update_data','n_clicks'),
               Input('hour_slider','value'),
               State('day_slider','value'),
               State('month_slider','value'),],
              prevent_initial_call=False)
def gen_inl_graph(clicks, hour_inp, day, month_val):
    
    #updates north forecast
    north_forc_fig, north_power, predicted_north_flow = plot_forecast(data_df,3,month_val, day, hour_inp, "north")
    
    #updates south forecast
    south_forc_fig, south_power, predicted_south_flow = plot_forecast(data_df,3,month_val, day, hour_inp, "south")
    
    #finds online pumps from optimization
    online_wells, north_q, south_q, north_usage, south_usage = update_optimization(init_df,predicted_north_flow, predicted_south_flow, hour_inp)
    
    #updates greographic plot based on online wells
    fig = generate_geo_plot(locations_df,online_wells)
    
    #gets north and south data to be displayed in the prediction tables.
    north_data, south_data = update_table(locations_df,online_wells)
    
    #formats data to be displayed in north table in user interface.
    north_df = [html.Div([
        dash_table.DataTable(id = "north_on_pumps",
            data=north_data.to_dict('records'),
            columns=[{'name': i, 'id': i} for i in north_data.columns],
            editable=False)])]
    
    #formats data to be displayed in south table in user interface.
    south_df = [html.Div([
        dash_table.DataTable(id = "south_on_pump",
            data=south_data.to_dict('records'),
            columns=[{'name': i, 'id': i} for i in south_data.columns],
            editable=False)])]
    
    return fig, north_df, south_df, round(predicted_north_flow,1), round(predicted_south_flow,1), north_forc_fig, round(north_power,1), south_forc_fig, round(south_power,1)


#updates number of days in month based on selection fro month.
@app.callback([Output(component_id='day_slider', component_property='max'),
              Output(component_id='day_slider', component_property='marks')],
              [Input(component_id='month_slider', component_property='value')],
              prevent_initial_call=False)
def update_days_callback(month_val):
    #print(month_val)
      
    days_dict = update_days(month_key[month_val][1])
    
    return month_key[month_val][1], days_dict
    
if __name__ == '__main__':
    app.run_server(debug=False)
