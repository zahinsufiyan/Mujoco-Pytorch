import gym
import numpy as np
import argparse
import os

from environment import NormalizedGymEnv
from utils import ReplayBuffer, convert_to_tensor, make_transition

os.makedirs('./model_weights', exist_ok=True)

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions.normal import Normal

class Actor(nn.Module):
    def __init__(self,state_dim, action_dim, hidden_dim):
        super(Actor, self).__init__()
        self.fc1   = nn.Linear(state_dim,hidden_dim)
        self.fc2   = nn.Linear(hidden_dim,hidden_dim)

        self.pi = nn.Linear(hidden_dim,action_dim)
        self.actor_logstd = nn.Parameter(torch.zeros(1, action_dim))
        for layer in self.modules():
            if isinstance(layer, nn.Linear):
                nn.init.orthogonal_(layer.weight)
                layer.bias.data.zero_()   
                
    def forward(self,x):
        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        mu = self.pi(x)
        std = torch.exp(self.actor_logstd)
        return mu,std
    
class QNetwork(nn.Module):
    def __init__(self,state_dim,action_dim,hidden_dim):
        super(QNetwork, self).__init__()
        self.fc1   = nn.Linear(state_dim+action_dim,hidden_dim)
        self.fc2   = nn.Linear(hidden_dim,hidden_dim)
        
        self.q  = nn.Linear(hidden_dim,1)
        
        for layer in self.modules():
            if isinstance(layer, nn.Linear):
                nn.init.orthogonal_(layer.weight)
                layer.bias.data.zero_()       
                
    def forward(self,state,action):
        x = torch.cat((state,action),-1)
        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        q = self.q(x)
        return q
    
class Args():
    def __init__(self):
        self.env_name = 'Hopper-v2'
        self.train = True
        self.render = False
        self.hidden_dim = 256
        self.epochs = 5000
        self.minibatch_size = 64
        self.tensorboard = True
        self.load = 'no'
        self.save_interval = 100
        self.print_interval = 10
        self.use_cuda = False
args = Args()

class SAC(nn.Module):
    def __init__(self, state_dim, action_dim, hidden_dim):
        super(SAC,self).__init__()
        self.q_1 = QNetwork(state_dim,action_dim,hidden_dim)
        self.q_2 = QNetwork(state_dim,action_dim,hidden_dim)
        
        self.target_q_1 = QNetwork(state_dim,action_dim,hidden_dim)
        self.target_q_2 = QNetwork(state_dim,action_dim,hidden_dim)
        
        self.soft_update(self.q_1, self.target_q_1, 1.)
        self.soft_update(self.q_2, self.target_q_2, 1.)
        self.actor = Actor(state_dim,action_dim,hidden_dim)
        
        self.alpha = nn.Parameter(torch.tensor(0.2))
        self.data = ReplayBuffer(action_prob_exist = False, max_size = int(1e+6), state_dim = state_dim, num_action = action_dim)
        self.target_entropy = -torch.tensor(action_dim)
        
        self.gamma = 0.99
        self.lr_q = 3e-4
        self.lr_pi = 3e-4
        self.lr_alpha = 3e-4
        self.device = 'cpu'
        
        self.soft_update_rate = 0.005
        
        self.q_1_optimizer = optim.Adam(self.q_1.parameters(), lr=self.lr_q)
        self.q_2_optimizer = optim.Adam(self.q_2.parameters(), lr=self.lr_q)
        
        self.actor_optimizer = optim.Adam(self.actor.parameters(), lr=self.lr_pi)
        self.alpha_optimizer = optim.Adam([self.alpha], lr=self.lr_alpha)
        
    def put_data(self,transition):
        self.data.put_data(transition)
        
    def soft_update(self, network, target_network, rate):
        for network_params, target_network_params in zip(network.parameters(), target_network.parameters()):
            target_network_params.data.copy_(target_network_params.data * (1.0 - rate) + network_params.data * rate)
    
    def get_action(self,state):
        mu,std = self.actor(state)
        dist = Normal(mu,std)
        u = dist.rsample()
        u_log_prob = dist.log_prob(u)
        a = torch.tanh(u)
        a_log_prob = u_log_prob - torch.log(1 - torch.square(a) +1e-3)
        return a, a_log_prob.sum(-1, keepdim=True)
    
    def forward(self,x):
        return x
    
    def train_net(self,batch_size,writer,n_epi):
        data = self.data.sample(shuffle = True, batch_size = batch_size)
        states, actions, rewards, next_states, done_masks = convert_to_tensor(self.device, data['state'], data['action'], data['reward'], data['next_state'], data['done'])
        ###target
        next_actions, next_action_log_prob = self.get_action(next_states)
        q_1 = self.target_q_1(next_states,next_actions)
        q_2 = self.target_q_2(next_states,next_actions)
        q = torch.min(q_1,q_2)
        v = done_masks * (q - self.alpha * next_action_log_prob)
        target = rewards + self.gamma * v

        ###q update
        self.q_1_optimizer.zero_grad()
        q_1 = self.q_1(states,actions)
        q_1_loss = F.smooth_l1_loss(q_1, target.detach())
        if writer != None:
            writer.add_scalar("loss/q_1", q_1_loss, n_epi)
        q_1_loss.backward()
        self.q_1_optimizer.step()
        
        self.q_2_optimizer.zero_grad()
        q_2 = self.q_2(states,actions)
        q_2_loss = F.smooth_l1_loss(q_2, target.detach())
        if writer != None:
            writer.add_scalar("loss/q_2", q_2_loss, n_epi)
        q_2_loss.backward()
        self.q_2_optimizer.step()
        
        ### actor update
        now_actions, now_action_log_prob = self.get_action(states)
        q_1 = self.q_1(states,now_actions)
        q_2 = self.q_2(states,now_actions)
        q = torch.min(q_1,q_2)
        
        self.actor_optimizer.zero_grad()
        actor_loss = (self.alpha.detach() * now_action_log_prob - q.detach())
        if writer != None:
            writer.add_scalar("loss/actor_loss", actor_loss.mean(), n_epi)
        actor_loss.mean().backward()
        self.actor_optimizer.step()
        
        ###alpha update
        self.alpha_optimizer.zero_grad()
        alpha_loss = - self.alpha * (now_action_log_prob + self.target_entropy).detach()
        if writer != None:
            writer.add_scalar("loss/alpha_loss", alpha_loss.mean(), n_epi)
        alpha_loss.mean().backward()
        self.alpha_optimizer.step()
        
        self.soft_update(self.q_1, self.target_q_1, self.soft_update_rate)
        self.soft_update(self.q_2, self.target_q_2, self.soft_update_rate)
        
env_lst = ['Ant-v2','HalfCheetah-v2', 'Hopper-v2', 'Humanoid-v2', 'HumanoidStandup-v2',\
          'InvertedDoublePendulum-v2', 'InvertedPendulum-v2', 'Walker2d-v2', 'Swimmer-v2', 'Reacher-v2']

assert args.env_name in env_lst

env = NormalizedGymEnv(args.env_name,normalize_obs=True)

'''
#for pybullet envs
import pybullet_envs
env = NormalizedGymEnv("HopperBulletEnv-v0",normalize_obs=True)
'''

action_space = env.action_space.shape[0]
state_space = env.observation_space.shape[0]
device = 'cuda' if torch.cuda.is_available() else 'cpu'
if args.use_cuda == False:
    device = 'cpu'

agent = SAC(state_space,action_space,args.hidden_dim)

if (torch.cuda.is_available()) and (args.use_cuda):
    agent = agent.cuda()

if args.load != 'no':
    agent.load_state_dict(torch.load("./model_weights/"+args.load))

if args.tensorboard:
    from torch.utils.tensorboard import SummaryWriter
    writer = SummaryWriter()
else:
    writer = None

score_lst = []


for n_epi in range(args.epochs):
    state = (env.reset())
    done = False
    score = 0.0
    while not done:
        if args.render:    
            env.render()
        action, _ = agent.get_action(torch.from_numpy(state).float().to(device))
        action = action.cpu().detach().numpy()
        next_state, reward, done, info = env.step(action)

        transition = make_transition(state,\
                                     action,\
                                     np.array([reward/10.0]),\
                                     next_state,\
                                     np.array([done])\
                                    )
        agent.put_data(transition) 

        state = next_state
        
        score += reward
        if agent.data.data_idx > 3000:
            agent.train_net(args.minibatch_size, writer, n_epi)  
    score_lst.append(score)
    if args.tensorboard:
        writer.add_scalar("score/score", score, n_epi)
    if n_epi%args.print_interval==0 and n_epi!=0:
        print("# of episode :{}, avg score : {:.1f}".format(n_epi, sum(score_lst)/len(score_lst)))
        score_lst = []