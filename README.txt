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
