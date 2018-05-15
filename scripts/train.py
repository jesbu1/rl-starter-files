#!/usr/bin/env python3

import argparse
import gym
import gym_minigrid
import time
import datetime
import sys
import torch
import torch_rl

import utils

# Parse arguments

parser = argparse.ArgumentParser()
parser.add_argument("--algo", required=True,
                    help="algorithm to use: a2c | ppo (REQUIRED)")
parser.add_argument("--env", required=True,
                    help="name of the environment to train on (REQUIRED)")
parser.add_argument("--model", default=None,
                    help="name of the model (default: ENV_ALGO_TIME)")
parser.add_argument("--seed", type=int, default=1,
                    help="random seed (default: 1)")
parser.add_argument("--procs", type=int, default=16,
                    help="number of processes (default: 16)")
parser.add_argument("--frames", type=int, default=10**7,
                    help="number of frames of training (default: 10e7)")
parser.add_argument("--log-interval", type=int, default=1,
                    help="number of updates between two logs (default: 1)")
parser.add_argument("--save-interval", type=int, default=0,
                    help="number of updates between two saves (default: 0, 0 means no saving)")
parser.add_argument("--no-tb", action="store_true", default=False,
                    help="don't log into tensorboard")
parser.add_argument("--frames-per-proc", type=int, default=None,
                    help="number of frames per process before update (default: 5 for A2C and 128 for PPO)")
parser.add_argument("--discount", type=float, default=0.99,
                    help="discount factor (default: 0.99)")
parser.add_argument("--lr", type=float, default=7e-4,
                    help="learning rate (default: 7e-4)")
parser.add_argument("--gae-tau", type=float, default=0.95,
                    help="tau coefficient in GAE formula (default: 0.95, 1 means no gae)")
parser.add_argument("--entropy-coef", type=float, default=0.01,
                    help="entropy term coefficient (default: 0.01)")
parser.add_argument("--value-loss-coef", type=float, default=0.5,
                    help="value loss term coefficient (default: 0.5)")
parser.add_argument("--max-grad-norm", type=float, default=0.5,
                    help="maximum norm of gradient (default: 0.5)")
parser.add_argument("--recurrence", type=int, default=1,
                    help="number of timesteps gradient is backpropagated (default: 1)")
parser.add_argument("--optim-eps", type=float, default=1e-5,
                    help="Adam and RMSprop optimizer epsilon (default: 1e-5)")
parser.add_argument("--optim-alpha", type=float, default=0.99,
                    help="RMSprop optimizer apha (default: 0.99)")
parser.add_argument("--clip-eps", type=float, default=0.2,
                    help="clipping epsilon for PPO (default: 0.2)")
parser.add_argument("--epochs", type=int, default=4,
                    help="number of epochs for PPO (default: 4)")
parser.add_argument("--batch-size", type=int, default=256,
                    help="batch size for PPO (default: 256, 0 means all)")
args = parser.parse_args()

# Set seed for all randomness sources

utils.seed(args.seed)

# Generate environments

envs = []
for i in range(args.procs):
    env = gym.make(args.env)
    env.seed(args.seed + i)
    envs.append(env)

# Define model name

suffix = datetime.datetime.now().strftime("%y%m%d%H%M%S")
model_name = args.model or args.env + "_" + args.algo + "_" + suffix

# Define obss preprocessor

obss_preprocessor = utils.ObssPreprocessor(model_name, envs[0].observation_space)

# Define actor-critic model

acmodel = utils.load_model(obss_preprocessor.obs_space, envs[0].action_space, model_name)
if torch.cuda.is_available():
    acmodel.cuda()

# Define actor-critic algo

if args.algo == "a2c":
    algo = torch_rl.A2CAlgo(envs, acmodel, args.frames_per_proc, args.discount, args.lr, args.gae_tau,
                            args.entropy_coef, args.value_loss_coef, args.max_grad_norm, args.recurrence,
                            args.optim_alpha, args.optim_eps, obss_preprocessor, utils.reshape_reward)
elif args.algo == "ppo":
    algo = torch_rl.PPOAlgo(envs, acmodel, args.frames_per_proc, args.discount, args.lr, args.gae_tau,
                            args.entropy_coef, args.value_loss_coef, args.max_grad_norm, args.recurrence,
                            args.optim_eps, args.clip_eps, args.epochs, args.batch_size, obss_preprocessor,
                            utils.reshape_reward)
else:
    raise ValueError

# Define logger and tensorboard writer then log command and model

log_name = model_name + ("_" + suffix if args.model is not None else "")

logger = utils.Logger(log_name)
if not(args.no_tb):
    from tensorboardX import SummaryWriter
    writer = SummaryWriter(utils.get_log_path(log_name, ext=False))

logger.log(" ".join(sys.argv), to_print=False)
logger.log(acmodel)

# Train model

num_frames = 0
total_start_time = time.time()
i = 0

while num_frames < args.frames:
    # Update parameters

    update_start_time = time.time()
    log = algo.update_parameters()
    update_end_time = time.time()
    
    num_frames += log["num_frames"]
    i += 1

    # Print logs

    if i % args.log_interval == 0:
        total_ellapsed_time = int(time.time() - total_start_time)
        fps = log["num_frames"]/(update_end_time - update_start_time)
        duration = datetime.timedelta(seconds=total_ellapsed_time)
        return_per_episode = utils.synthesize(log["return_per_episode"])
        rreturn_per_episode = utils.synthesize(log["reshaped_return_per_episode"])
        num_frames_per_episode = utils.synthesize(log["num_frames_per_episode"])

        logger.log(
            "U {} | F {:06} | FPS {:04.0f} | D {} | rR:x̄σmM {: .2f} {: .2f} {: .2f} {: .2f} | F:x̄σmM {:.1f} {:.1f} {:.1f} {:.1f} | H {:.3f} | V {:.3f} | pL {: .3f} | vL {:.3f}"
            .format(i, num_frames, fps, duration,
                    *rreturn_per_episode.values(),
                    *num_frames_per_episode.values(),
                    log["entropy"], log["value"], log["policy_loss"], log["value_loss"]))
        if not(args.no_tb):
            writer.add_scalar("frames", num_frames, i)
            writer.add_scalar("FPS", fps, i)
            writer.add_scalar("duration", total_ellapsed_time, i)
            for key, value in return_per_episode.items():
                writer.add_scalar("return_" + key, value, i)
            for key, value in rreturn_per_episode.items():
                writer.add_scalar("rreturn_" + key, value, i)
            for key, value in num_frames_per_episode.items():
                writer.add_scalar("num_frames_" + key, value, i)
            writer.add_scalar("entropy", log["entropy"], i)
            writer.add_scalar("value", log["value"], i)
            writer.add_scalar("policy_loss", log["policy_loss"], i)
            writer.add_scalar("value_loss", log["value_loss"], i)

    # Save obss preprocessor vocabulary and model

    if args.save_interval > 0 and i % args.save_interval == 0:
        obss_preprocessor.vocab.save()

        if torch.cuda.is_available():
            acmodel.cpu()
        utils.save_model(acmodel, model_name)
        if torch.cuda.is_available():
            acmodel.cuda()