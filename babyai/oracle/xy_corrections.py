import numpy as np
import pickle as pkl
from babyai.oracle.teacher import Teacher


class XYCorrections(Teacher):
    def __init__(self, *args, **kwargs):
        super(XYCorrections, self).__init__(*args, **kwargs)

    def empty_feedback(self):
        """
        Return a tensor corresponding to no feedback.
        """
        return np.zeros(4) - 1

    def random_feedback(self):
        """
        Return a tensor corresponding to no feedback.
        """
        return np.random.uniform(0, 1, size=4)

    def compute_feedback(self, oracle):
        """
        Return the expert action from the previous timestep.
        """
        self.step_ahead(oracle)
        return np.array(self.next_state_coords)

    # TODO: THIS IS NO IMPLEMENTED FOR THIS TEACHER! IF WE END UP USING THIS METRIC, WE SHOULD MAKE IT CORRECT!
    def success_check(self, state, action, oracle):
        return True

    def step_ahead(self, oracle):
        original_teacher = oracle.mission.teacher
        oracle.mission.teacher = None
        env = pkl.loads(pkl.dumps(oracle.mission))
        env.teacher = None
        try:
            self.next_state, self.next_state_coords = self.step_away_state(env, oracle, self.cartesian_steps)
            # Coords are quite large, so normalize them to between [-1, 1]
            self.next_state_coords[:2] = (self.next_state_coords[:2] - 12) / 12
            # Also normalize direction
            self.next_state_coords[2] = self.next_state_coords[3] - 2
        except Exception as e:
            print("STEP AWAY FAILED!", e)
            self.next_state = self.next_state * 0
            self.next_state_coords = -1 * np.ones(4)
            self.last_step_error = True
        oracle.mission.teacher = original_teacher
        return oracle
