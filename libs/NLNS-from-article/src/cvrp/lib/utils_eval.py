import numpy as np
import json
import random
import math
import vrp_env
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Data,DataLoader
from lib.rms import RunningMeanStd
from arguments import args
import argparse
args = args()

device = torch.device(args.device)
N_JOBS = int(args.N_JOBS)
CAP = int(args.CAP)
batch_size = min(int(args.BATCH), np.load("data/cvrp_99.npy").shape[0])
MAX_COORD = int(args.MAX_COORD)
MAX_DIST = float(args.MAX_DIST)
LR = float(args.LR)

N_ROLLOUT = int(args.N_ROLLOUT)
ROLLOUT_STEPS = int(args.ROLLOUT_STEPS)
N_STEPS = int(args.N_STEPS)

init_T=float(args.init_T)
final_T=float(args.final_T)

reward_norm = RunningMeanStd()

def create_instance(index):
    coords = np.load("data/cvrp_99.npy")[index]
    coords = coords.tolist()

    jobs = []
    for i,(x,y,demand) in enumerate(coords[1:]):
        jobs.append({
                "id": i,
                "loc": i+1,
                "name": str(i),
                "x":x,
                "y":y,
                "weight":demand,
                "tw": {
                    "start": 0,
                    "end": 10000,
                },
                "service_time":0,
                "job_type": "Pickup",
            })

    def calc_dist(l,r):
        return ((l[0]-r[0])**2 + (l[1]-r[1])**2)**0.5

    dist_time = []

    for i,(x1,y1,_) in enumerate(coords):
        row = []
        for j,(x2,y2,_) in enumerate(coords):
            d = calc_dist((x1,y1),(x2,y2))
            row.append(({"dist":d,"time": d}))
        dist_time.append(row)

    v = {
        "cap": CAP,
        "tw": {
            "start": 0,
            "end": 10000,
        },
        "start_loc": 0,
        "end_loc": 0,
        "fee_per_dist": 1.0,
        "fee_per_time": 0,
        "fixed_cost": 0,
        "handling_cost_per_weight": 0.0,
        "max_stops": 0,
        "max_dist": 0,
    }

    alpha_T = (final_T/init_T)**(1.0/N_STEPS)
    input_data = {
        "vehicles": [v],
        "dist_time": dist_time,
        "cost_per_absent": 1000,
        "jobs": jobs,
        "depot": coords[0][:2],
        "l_max": 10,
        "c1": 10,
        "adjs": [],
        "temperature": 100,
        "c2": alpha_T,
        "sa": True,
    }

    return input_data

def create_env(index):

    class Env(object):
        def __init__(self,index,n_jobs=99,_input=None):
            self.n_jobs = n_jobs
            if _input == None:
                _input = create_instance(index)

            self.input = _input
            dist_time = _input['dist_time']
            self.dists = np.array([[ [x['dist']/MAX_DIST] for x in row ] for row in dist_time])

        def reset(self):
            self.env = vrp_env.Env(json.dumps(self.input))
            self.mapping = {}
            self.cost = 0.0
            self.best = None
            return self.get_states()


        def get_states(self):
            states = self.env.states()
            tours = self.env.tours()
            jobs = self.input['jobs']
            depot = self.input['depot']

            nodes = np.zeros((self.n_jobs+1,4))
            edges = np.zeros((self.n_jobs+1,self.n_jobs+1,1))
            mapping = {}

            for i,(tour,tour_state) in enumerate(zip(tours,states)):
                for j,(index,s) in enumerate(zip(tour,tour_state[1:])):
                    job = jobs[index]
                    loc = job['loc']
                    nodes[loc,:] = [job['weight']/CAP,s['weight']/CAP,s['dist']/MAX_DIST,s['time']/MAX_DIST]
                    mapping[loc] = (i,j)


            for tour in tours:
                edges[0][tour[0]+1][0] = 1
                for l,r in zip(tour[0:-1],tour[1:]):
                    edges[l+1][r+1][0] = 1
                edges[tour[-1]+1][0][0] = 1

            edges = np.stack([self.dists,edges],axis=-1)
            edges = edges.reshape(-1,2)

            self.mapping = mapping
            self.cost = self.env.cost()
            if self.best is None or self.cost < self.best:
                self.best = self.cost

            return nodes,edges

        def step(self,to_remove):
            prev_cost = self.cost
            self.env.step(to_remove)
            nodes,edges = self.get_states()
            reward = prev_cost - self.cost
            return nodes,edges,reward


    env = Env(index)
    return env

def create_batch_env(batch_size=batch_size):

    class BatchEnv(object):
        def __init__(self,batch_size=batch_size):
#             _input = create_instance(n_jobs+1)
            self.envs = [ create_env(i) for i in range(batch_size) ]

        def reset(self):
            rets = [ env.reset() for env in self.envs ]
            return list(zip(*rets))

        def step(self,actions):
            actions = actions.tolist()
            assert(len(actions) == len(self.envs))
            rets = [env.step(act) for env,act in zip(self.envs,actions)]
            return list(zip(*rets))

    return BatchEnv(batch_size)

def create_replay_buffer(n_jobs=99):

    class Buffer(object):
        def __init__(self,n_jobs=n_jobs):
            super(Buffer,self).__init__()
            self.buf_nodes = []
            self.buf_edges = []
            self.buf_actions = []
            self.buf_rewards = []
            self.buf_values = []
            self.buf_log_probs = []
            self.n_jobs = n_jobs

            edges = []
            for i in range(n_jobs+1):
                for j in range(n_jobs+1):
                    edges.append([i,j])

            self.edge_index = torch.LongTensor(edges).T

        def obs(self,nodes,edges,actions,rewards,log_probs,values):
            self.buf_nodes.append(nodes)
            self.buf_edges.append(edges)
            self.buf_actions.append(actions)
            self.buf_rewards.append(rewards)
            self.buf_values.append(values)
            self.buf_log_probs.append(log_probs)

        def compute_values(self,last_v=0,_lambda = 1.0):
            rewards = np.array(self.buf_rewards)
#             rewards = (rewards - rewards.mean()) / rewards.std()
            pred_vs = np.array(self.buf_values)

            target_vs = np.zeros_like(rewards)
            advs = np.zeros_like(rewards)

#             print (rewards.shape,target_vs.shape,advs.shape,pred_vs.shape)

            v = last_v
            for i in reversed(range(rewards.shape[0])):
                v = rewards[i] + _lambda * v
                target_vs[i] = v
                adv = v - pred_vs[i]
                advs[i] = adv

            return target_vs,advs

        def gen_datas(self,last_v=0,_lambda = 1.0,batch_size=batch_size):
            target_vs,advs = self.compute_values(last_v,_lambda)
            advs = (advs - advs.mean()) / advs.std()
            l,w = target_vs.shape

            datas = []
            for i in range(l):
                for j in range(w):
                    nodes = self.buf_nodes[i][j]
                    edges = self.buf_edges[i][j]
                    action = self.buf_actions[i][j]
                    v = target_vs[i][j]
                    adv = advs[i][j]
                    log_prob = self.buf_log_probs[i][j]
#                     print (nodes.dtype,self.edge_index.dtype,edges.dtype,q,action)
                    data = Data(x=torch.from_numpy(nodes).float(),edge_index=self.edge_index,
                                edge_attr=torch.from_numpy(edges).float(),v=torch.tensor([v]).float(),
                                action=torch.tensor(action).long(),
                                log_prob=torch.tensor([log_prob]).float(),
                                adv = torch.tensor([adv]).float())
                    datas.append(data)

            return datas

        def create_data(self,_nodes,_edges):
            datas = []
            l = len(_nodes)
            for i in range(l):
                nodes = _nodes[i]
                edges = _edges[i]
                data = Data(x=torch.from_numpy(nodes).float(),edge_index=self.edge_index,edge_attr=torch.from_numpy(edges).float())
                datas.append(data)
            dl = DataLoader(datas,batch_size=l)
            return list(dl)[0]

    return Buffer()

def roll_out(model,envs,states,n_steps=10,_lambda=0.99,batch_size=batch_size,is_last=False,greedy=False):
    buffer = create_replay_buffer()
    with torch.no_grad():
        model.eval()
        nodes,edges = states
        _sum = 0
        _entropy = []

        history = []
        for i in range(n_steps):
            data = buffer.create_data(nodes,edges)
            data = data.to(device)
            actions,log_p,values,entropy = model(data,10,greedy)
#             print (values.shape)
            new_nodes,new_edges,rewards = envs.step(actions.cpu().numpy())
            rewards = np.array(rewards)
            _sum = _sum + rewards
            rewards = reward_norm(rewards)
            _entropy.append(entropy.mean().cpu().numpy())

            buffer.obs(nodes,edges,actions.cpu().numpy(),rewards,log_p.cpu().numpy(),values.cpu().numpy())
            nodes,edges = new_nodes,new_edges
            history.append([env.cost for env in envs.envs])

        mean_value = _sum.mean()
#         print ("mean rewards:",mean_value)
#         print ("entropy:",np.mean(_entropy))
#         print ("mean cost:",np.mean([env.cost for env in envs.envs]))

        if not is_last:
#             print ("not last")
            data = buffer.create_data(nodes,edges)
            data = data.to(device)
            actions,log_p,values,entropy = model(data,10,greedy)
            values = values.cpu().numpy()
        else:
            values = 0

        dl = buffer.gen_datas(values,_lambda = _lambda,batch_size=batch_size)
        return dl,(nodes,edges),history
