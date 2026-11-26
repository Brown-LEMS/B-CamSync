"""Experimental alternative to Tracker using a filtered phase and one outstanding impulse."""
from math import exp, remainder


class CenteredTracker:
    def __init__(self, probes, model, fps, lag=10):
        self.reference, self.follower = probes
        self.period, self.rate, self.negative, self.positive = model
        self.alpha = 1 - exp(-1 / (0.5*fps))
        self.beta = self.alpha**2 / 4
        self.lag = lag
        self.frame = 0
        self.phase = None
        self.due = -100
        self.delta = 0
        self.guard = 3
        self.min_rate = 0.4/fps  # Drift estimate below 0.4 µs/s is directionally ambiguous.

    def request(self):
        self.frame += 1
        phase = remainder((self.follower.latest - self.reference.latest)/1000, self.period)
        if self.phase is None:
            self.phase = phase
        else:
            self.phase += self.rate
        if self.frame == self.due:
            self.phase += self.delta
        # Frozen measurement updates across the bounded request pipeline keep old
        # timestamps from creating an immediate opposite correction after a pulse.
        if self.frame <= self.due + self.guard:
            return self.follower.baseline_us
        innovation = remainder(phase - self.phase, self.period)
        self.phase += self.alpha*innovation
        self.rate += self.beta*innovation
        predicted = self.phase + self.lag*self.rate
        cmd, move = self.negative if predicted > 0 else self.positive
        # Near zero, correct only when estimated phase is moving away from zero;
        # for a large residual, either direction can recover from a disturbance.
        moving_away = abs(self.rate) > self.min_rate and self.rate*predicted > 0
        trigger = 0.5 if moving_away else 0.9
        if abs(predicted) >= trigger * abs(move):
            self.due, self.delta = self.frame+self.lag, move
            return self.follower.baseline_us + cmd
        return self.follower.baseline_us
