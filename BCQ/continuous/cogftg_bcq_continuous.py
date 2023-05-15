#!/usr/bin/env python3

import argparse
import datetime
import os
import pprint

import gymnasium as gym
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from gym import spaces
from tianshou.data import Collector
from tianshou.trainer import offline_trainer
from offline_trainer import offline_trainer
from tianshou.utils import TensorboardLogger, WandbLogger
from tianshou.utils.net.common import MLP, Net
from gym import spaces #add

"""
    make sure you have those files in the directory 
"""
from utils_bcq import load_buffer_ftg,normalize_all_obs_in_replay_buffer #add
from offline_trainer import offline_trainer#from tianshou.trainer import offline_trainer
from continuous import Critic,VAE, Perturbation
from fight_agent import get_sound_encoder,STATE_DIM
from bcq import BCQPolicy


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, default="BCQ-continuous")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--expert-data-task", type=str, default="halfcheetah-expert-v2"
    )
    parser.add_argument("--buffer-size", type=int, default=1000000)
    parser.add_argument("--hidden-sizes", type=int, nargs="*", default=[256, 256])
    parser.add_argument("--actor-lr", type=float, default=3e-4)
    parser.add_argument("--critic-lr", type=float, default=1e-7)
    parser.add_argument("--start-timesteps", type=int, default=10000)
    parser.add_argument("--epoch", type=int, default=200)
    parser.add_argument("--step-per-epoch", type=int, default=5000)
    parser.add_argument("--n-step", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--test-num", type=int, default=10)
    parser.add_argument("--logdir", type=str, default="log")
    parser.add_argument("--render", type=float, default=1 / 35)

    parser.add_argument("--vae-hidden-sizes", type=int, nargs="*", default=[512, 512])
    # default to 2 * action_dim
    parser.add_argument("--latent-dim", type=int)
    parser.add_argument("--gamma", default=0.99)
    parser.add_argument("--tau", default=0.005)
    # Weighting for Clipped Double Q-learning in BCQ
    parser.add_argument("--lmbda", default=0.75)
    # Max perturbation hyper-parameter for BCQ
    parser.add_argument("--phi", default=0.05)
    parser.add_argument(
        "--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu"
    )
    parser.add_argument("--norm-obs", type=int, default=1)
    #TODO: load previous policy
    parser.add_argument("--resume-path", type=str)
    parser.add_argument("--resume-id", type=str, default=None)
    parser.add_argument(
        "--logger",
        type=str,
        default="tensorboard",
        choices=["tensorboard", "wandb"],
    )
    parser.add_argument("--wandb-project", type=str, default="offline_d4rl.benchmark")
    parser.add_argument(
        "--watch",
        default=False,
        action="store_true",
        help="watch the play of pre-trained policy only",
    )
    return parser.parse_args()


def test_bcq():
    args = get_args()
    # env = gym.make(args.task)
    n_frame = 1
    observation_space=spaces.Box(low=-1.9, high=1.9, shape=(STATE_DIM[n_frame]['mel'],))
    action_space = spaces.Box(low=0, high=1, shape=(40,)) #add
    args.state_shape = observation_space.shape #env.observation_space.shape or env.observation_space.n
    args.action_shape = action_space.shape #env.action_space.shape or env.action_space.n
    args.max_action = action_space.high[0] #env.action_space.high[0]  # float
    print("device:", args.device)
    print("Observations shape:", args.state_shape)
    print("Actions shape:", args.action_shape)
    print("Action range:", np.min(action_space.low), np.max(action_space.high))#delete env

    args.state_dim = args.state_shape[0]
    args.action_dim = args.action_shape[0]
    print("Max_action", args.max_action)

    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # model
    encoder=get_sound_encoder('mel')
    # perturbation network, add encoder into actor 
    net_a = MLP(
        input_dim=args.state_dim + args.action_dim,
        output_dim=args.action_dim,
        hidden_sizes=args.hidden_sizes,
        device=args.device,
    )
    actor = Perturbation(
        net_a, 
        max_action=args.max_action, 
        device=args.device, 
        phi=args.phi,
        encoder=encoder
    ).to(args.device)
    actor_optim = torch.optim.Adam(actor.parameters(), lr=args.actor_lr)

    net_c1 = Net(
        args.state_shape,
        args.action_shape,
        hidden_sizes=args.hidden_sizes,
        concat=True,
        device=args.device,
    )
    net_c2 = Net(
        args.state_shape,
        args.action_shape,
        hidden_sizes=args.hidden_sizes,
        concat=True,
        device=args.device,
    )
    critic1 = Critic(net_c1, device=args.device,encoder=encoder).to(args.device)
    critic1_optim = torch.optim.Adam(critic1.parameters(), lr=args.critic_lr)
    critic2 = Critic(net_c2, device=args.device,encoder=encoder).to(args.device)
    critic2_optim = torch.optim.Adam(critic2.parameters(), lr=args.critic_lr)

    # vae
    # output_dim = 0, so the last Module in the encoder is ReLU
    vae_encoder = MLP(
        input_dim=args.state_dim + args.action_dim,
        hidden_sizes=args.vae_hidden_sizes,
        device=args.device,
    )
    if not args.latent_dim:
        args.latent_dim = args.action_dim * 2
    vae_decoder = MLP(
        input_dim=args.state_dim + args.latent_dim,
        output_dim=args.action_dim,
        hidden_sizes=args.vae_hidden_sizes,
        device=args.device,
    )
    vae = VAE(
        vae_encoder,
        vae_decoder,
        hidden_dim=args.vae_hidden_sizes[-1],
        latent_dim=args.latent_dim,
        max_action=args.max_action,
        device=args.device,
        pre_encoder=encoder
    ).to(args.device)
    vae_optim = torch.optim.Adam(vae.parameters())

    policy = BCQPolicy(
        actor,
        actor_optim,
        critic1,
        critic1_optim,
        critic2,
        critic2_optim,
        vae,
        vae_optim,
        device=args.device,
        gamma=args.gamma,
        tau=args.tau,
        lmbda=args.lmbda,
    )

    # load a previous policy
    if args.resume_path:
        model_state_dict = torch.load(args.resume_path, map_location=args.device)
        policy.load_state_dict(model_state_dict)
        print("Loaded agent from: ", args.resume_path)
        torch.save(actor, "actor.pth")

    # collector
    test_collector = None# test_collector = Collector(policy, test_envs)

    # log
    now = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    args.algo_name = "bcq"
    log_name = os.path.join(args.task, args.algo_name, str(args.seed), now)
    log_path = os.path.join(args.logdir, log_name)

    # logger
    if args.logger == "wandb":
        logger = WandbLogger(
            save_interval=1,
            name=log_name.replace(os.path.sep, "__"),
            run_id=args.resume_id,
            config=args,
            project=args.wandb_project,
        )
    writer = SummaryWriter(log_path)
    writer.add_text("args", str(args))
    if args.logger == "tensorboard":
        logger = TensorboardLogger(writer)
    else:  # wandb
        logger.load(writer)

    def save_best_fn(policy,num=0):
        torch.save(policy.state_dict(), os.path.join(log_path, str(num)+"policy.pth")) 
        torch.save(actor, os.path.join(log_path, str(num)+"actor.pth")) 

    def watch():
        if args.resume_path is None:
            args.resume_path = os.path.join(log_path, "policy.pth")

        policy.load_state_dict(
            torch.load(args.resume_path, map_location=torch.device("cpu"))
        )
        policy.eval()
        collector = Collector(policy, env)
        collector.collect(n_episode=1, render=1 / 35)

    if not args.watch:
        replay_buffer = load_buffer_ftg(args.expert_data_task)
        if args.norm_obs:
            replay_buffer, obs_rms = normalize_all_obs_in_replay_buffer(replay_buffer)
            #test_envs.set_obs_rms(obs_rms)
        # trainer
        result = offline_trainer(
            policy,
            replay_buffer,
            test_collector,
            args.epoch,
            args.step_per_epoch,
            args.test_num,
            args.batch_size,
            save_best_fn=save_best_fn,
            logger=logger,
        )
        pprint.pprint(result)
    else:
        watch()

    # Let's watch its performance!
    # policy.eval()
    # test_envs.seed(args.seed)
    # test_collector.reset()
    # result = test_collector.collect(n_episode=args.test_num, render=args.render)
    # print(f"Final reward: {result['rews'].mean()}, length: {result['lens'].mean()}")


if __name__ == "__main__":
    test_bcq()
