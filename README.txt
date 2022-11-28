Water Well Pump Optimization Application

Description:
This tool provides operators with suggestions for which pumps to turn on or off in order to minimize electrical costs 
while maintaining reliable residential water supply. Additionally, it enables the end user to forecast the required 
changes to the system given a user-defined level of demand.

Installation:
1) Download or clone this git repository https://github.com/JacksonCorley/water_well_pump_optimization
2) Download Docker Desktop for your specific machine from https://www.docker.com/ 
3) Run Docker Desktop.
4) Unzip the water_well_pump_optimization.zip folder if you downloaded the above mentioned git 
repository as zip. Once unzipped, change your directory to the water_well_pump_optimization. 
cd directly to water_well_pump_optimization folder if you cloned the git repository. 
5) Open the terminal in water_well_pump_optimization directory.
6) Run the following command to run the application: 
    docker-compose up
7) Subsequently, the app can be accessed at,
    http://localhost:8000/


Folder structure of this application with file Description:
	--assets/style.css               -> contains the styles for the application
	--app.py            		 -> the main Dashboard application created using plotly Dash.
	--demand.ipynb		         -> jupyter Notebooks that tests our Time series Model and fills up the missing data in our dataset
	--docker-compose.yml		 -> Docker compose file to create our container
	--Dockerfile			 -> Docker file to create our image
	--Final_Data_hourly_clean.csv	 -> Finalized dataset
	--Final_Data_hourly.csv		 -> Initial dataset
	--forecast.py			 -> Timeseries model for forecasting
	--optimization.py		 -> Optimization code
	--readme.md			 -> Markdown readme file for github
	--README.txt			 -> this file
	--requirements.txt		 -> List of all the python packages being used in the project
	--Well_Summarys.csv		 -> List of all the pumps, their location, and their specs

How to use this Application:
1) Once you run the command docker-compose up, the app can be access through http://localhost:8000/ in any of your browser. 

Breakdown of UI:
1) Date Range Selection:
   In this section, you can select the start date and end date to get the forecast and optimized values. Unfortunately, the date range is limited to our available data, so this application cannot go past 7/7/19.

2) Hour Selection:
   The selected date range updates the hour selection field. You have the ability to get the forecast data in the increment of 1 hour between the selected date range.

3) Update Pump Projections:
   This button runs the date via our forecast time series model and optimizes the flow and cumulative power usage.

4) Cumulative Power Usage:
   This section highlights the optimized cumulative power usage vs the historical cumulative power usage for both north and south wells.

4) North Section: Combined Active Pumps:
   The section shows the line graph of north section including historical data and forecast data for both Flow and Cumulative power usage for the selected date range.

5) South Section: Combined Active Pumps:
   The section shows the line graph of south section including historical data and forecast data for both Flow and Cumulative power usage for the selected date range.

6) Well Operations Map:
   This section shows the pump location and active pumps.

7) North Well Operations Table:
   This section shows the north well active pumps their average flow and average power usage.

8) South Well Operations Table:
   This section shows the south well active pumps their average flow and average power usage.

9) Prediction Data:
   This section shows all the historical flow, predicted flow, historical power usage, and optimized power usage for both south and north wells.




