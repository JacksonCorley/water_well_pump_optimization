Water Well Pump Optimization Application

DESCRIPTION:
This tool provides operators with suggestions for which pumps to turn on or off in order to minimize electrical costs 
while maintaining reliable residential water supply. Additionally, it enables the end user to forecast the required 
changes to the system given a user-defined date range.

INSTALLATION :
1) Download Docker Desktop for your specific machine from https://www.docker.com/ 
1.a) Ensure that "docker-compose" is included with your Docker Desktop instalation. 
     If it isn't download it for your respective OS from https://docs.docker.com/compose/install/
2) Run Docker Desktop.
3) Open the terminal in team097final directory, then cd to CODE directory.
	cd CODE directory

EXECUTION :
1) Once inside CODE directory, run the following command to run the application: 
    docker-compose up
2) Subsequently, the app can be accessed at,
    http://localhost:8000/


DEMO VIDEO: https://youtu.be/H2ZkJvU6INA

Folder structure of CODE directory with file description:
	--assets/style.css               -> contains the styles for the application
	--app.py            		 -> the main Dashboard application created using plotly Dash.
	--docker-compose.yml		 -> Docker compose file to create our container
	--Dockerfile			 -> Docker file to create our image
	--Final_Data_hourly_clean.csv	 -> Finalized dataset
	--forecast.py			 -> Timeseries model for forecasting
	--optimization.py		 -> Determines optimal pump selection based power usage constrained by total flow. 
	--README.txt			 -> this file
	--requirements.txt		 -> List of all the python packages being used in the project
	--Well_Summarys.csv		 -> List of all the pumps, their location, and their specs

Breakdown of UI:
1) Date Range Selection:
   In this section, you can select the start date and end date to get the forecast and optimized values. Unfortunately, the date range is limited to our available data, so this application cannot go past 7/7/19 or before 7/9/2016.

2) Hour Selection:
   The selected date range updates the hour selection field. You have the ability to get the forecast data in the increment of 1 hour between the selected date range.

3) Update Pump Projections:
   When this button is pressed flow is forecast for the selected date range and the optimization algorithm is run for every hour in the selected date range.

4) Cumulative Power Usage:
   This section highlights the optimized cumulative power usage vs the historical cumulative power usage for both north and south wells for the entire selected date range.

4) North Section: Combined Active Pumps:
   The section shows a line graph of north section including historical data and forecast data for both Flow and Cumulative power usage for the selected date range.

5) South Section: Combined Active Pumps:
   The section shows a line graph of south section including historical data and forecast data for both Flow and Cumulative power usage for the selected date range.

6) Well Operations Map:
   This section shows the pump location and optmiized pump selection based on the hour chosen.

7) North Well Operations Table:
   This section shows the north well active pumps their average flow and average power usage based on the hour chosen.

8) South Well Operations Table:
   This section shows the south well active pumps their average flow and average power usage based on the hour chosen.

9) Prediction Data:
   This section shows all the historical flow, predicted flow, historical power usage, and optimized power usage for both south and north wells.



