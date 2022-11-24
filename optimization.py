#! /usr/bin/python3

import cvxpy as cp
import pandas as pd
import re


df = pd.read_csv("Well_Summarys.csv")
dat = df[["Location", "Section", "Average_Flow(gpm)", "Average_Power_Usage(kW-Hr)"]]
dfS = dat[df["Section"] == "South"]   # divide the dataset into north and south
dfN = dat[df["Section"] == "North"]   # divide the dataset into north and south


def optimizer(df, thresh=12584):
	#df["Average_Flow(gpm)"].loc[df["Average_Flow(gpm)"] == 0] = df["Average_Flow(gpm)"].mean()
	df = df[df["Average_Flow(gpm)"] != 0]
	df = df.reset_index(drop=True)
	#print(df)
	flow = df["Average_Flow(gpm)"]
	power = df["Average_Power_Usage(kW-Hr)"]
	pump = df["Location"]

	print("\n\nSection: {}\n\n".format(df["Section"][0]))

	q = [1 for i in range(len(flow))]
	k = [i for i in flow]
	pk = [i for i in power]
	x = cp.Variable(len(power), boolean=True)
	objective = cp.Minimize(pk @ x)
	constraints = [k @ x >= thresh]
	problem = cp.Problem(objective, constraints)
	result = problem.solve()
	pumpList = x.value
	print("---> Total power usage is {} kW-Hr to meet demand. <---".format(result))
	#print(pumpList)

	flowSum = 0
	for i,val in enumerate(pumpList):
		if val==1:
			flowSum += flow[i]
	
	print("---> Total flow to minimize power while meeting demand: {} gpm.".format(flowSum))
			
	out = list()
	pat = re.compile("\d\d?")
	for i in range(len(x.value)):
		if x.value[i] == 1:
			out.append("Pump {}".format(pat.search(pump[i]).group()))
	return out



df2 = [dfS, dfN]

for i in df2:
	optimal = optimizer(i)
	print("---> Optimal active pumps are shown below: <---")
	print(optimal)
