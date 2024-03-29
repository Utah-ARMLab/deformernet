#!/usr/bin/env python3

# import roslib; roslib.load_manifest('grasp_pipeline')
import rospy
from geometry_msgs.msg import Pose, Quaternion, PoseStamped, PointStamped
# from grasp_pipeline.srv import *
# from point_cloud_segmentation.srv import *
import os
# import tf
from sensor_msgs.msg import JointState, Image
import copy
import numpy as np
import time
import cv2
import time
from gazebo_msgs.msg import ModelStates
import h5py
from cv_bridge import CvBridge, CvBridgeError
import roslib.packages as rp
import sys
from dvrk_gazebo_control.srv import *
# import align_object_frame as align_obj
from point_cloud_segmentation.srv import *

# sys.path.append(rp.get_pkg_dir('grasp_pipeline') 
#                 + '/script')
# import plot_traj
# import robot_traj_interface
# from grasp_control.srv import *
# from ll4ma_robot_interface.srv import *
from std_srvs.srv import Trigger, TriggerRequest
# import align_object_frame as align_obj

from scipy.spatial.transform import Rotation as R

# import isaac_data_collection
ROBOT_Z_OFFSET = 0.25

class GraspDataCollectionClient:
    def __init__(self):
        # rospy.init_node('grasp_client')
        self.use_sim = rospy.get_param('~use_sim', True)
        self.use_hd = rospy.get_param('~use_hd', True)
        self.num_grasps_per_object = rospy.get_param('~num_grasps_per_object', 2)
        # rospy.loginfo("Doing %d grasps per object", self.num_grasps_per_object)
        self.save_visual_data_pre_path = rospy.get_param('~save_visual_data_pre_path', '/home/baothach/dvrk_grasp_data/visual/')
        self.smooth_plan_traj = rospy.get_param('~smooth_plan_traj', True)
        self.mount_desired_world = None
        self.mount_desired_world = None
        self.palm_desired_world = None
        self.object_world_seg_pose = None
        self.object_world_sim_pose = None
        self.table_len_z = 0.03  # Need fix
        self.min_palm_dist_to_table = rospy.get_param('~min_palm_dist_to_table', 0.)
        self.min_palm_height = self.table_len_z + self.min_palm_dist_to_table
        self.max_palm_dist_to_table = rospy.get_param('~max_palm_dist_to_table', 0.05)
        self.max_palm_height = self.table_len_z + self.max_palm_dist_to_table
        self.lift_height = 0.06
        lift_dist_suc_range = 0.02
        self.grasp_success_object_height = self.table_len_z + self.lift_height - lift_dist_suc_range
        self.place_x_min = -0.1
        self.place_x_max = 0.1
        self.place_y_min = 0.35
        self.place_y_max = 0.45
        self.state = "home"
        self.frame_count = 0
        self.grasp_label = None
        self.top_grasp_preshape_idx = None
        self.point_cloud = None # point cloud
        self.get_lift_moveit_plan = None
        self.grasp_id = 0
        self.grasp_plan_failures_num = 0
        self.max_grasp_plan_failures_num = 20
        self.traj_index = 0
        self.saved_object_state = None
        self.plan_traj = None
        self.height_before_lift = None # point in point cloud with max z before lift
        self.failed_grasp_plan = False

        self.data_recording_path = rospy.get_param('~data_recording_path', '/home/baothach/dvrk_grasp_data/')
        self.grasp_file_name = self.data_recording_path + 'grasp_data.h5'



    def set_up_object_name(self, object_name, object_mesh_path=None):
        self.object_name = object_name
        #self.grasp_id = grasp_id
        if object_mesh_path is not None:
            self.object_mesh_path = object_mesh_path


    def set_up_grasp_id(self, grasp_id):
        self.grasp_id = grasp_id


    def get_last_object_id_name(self):
        #bao123
        try:
            grasp_file = h5py.File(self.grasp_file_name, 'r')
            last_object_id = grasp_file['max_object_id'][()]
            self.last_object_name = grasp_file['cur_object_name'][()]
            self.cur_object_id = last_object_id + 1
        except (IOError, KeyError):
            try:
                grasp_file.close()
            except UnboundLocalError:
                pass
            rospy.loginfo('File Not Found, Creating New File')
            grasp_file = h5py.File(self.grasp_file_name, 'w')
            self.last_object_name = 'empty'
            self.cur_object_id = 0
        grasp_file.close()


    # def get_hand_joint_state(self, hand_js):
    #     self.true_hand_joint_state = hand_js 


    # def get_gazebo_rgb_image(self, rgb_image_msg):
    #     self.gazebo_rgb_image_msg = None
    #     try:
    #         self.gazebo_rgb_image_msg = rgb_image_msg
    #     except CvBridgeError as e:
    #         rospy.logerr(e)




    def create_moveit_scene_client(self, object_pose, mesh_scaling_factor = 1):
        rospy.loginfo('Waiting for service create_moveit_scene.')
        rospy.wait_for_service('create_moveit_scene')
        rospy.loginfo('Calling service create_moveit_scene.')
        try:
            create_scene_proxy = rospy.ServiceProxy('create_moveit_scene', ManageMoveitScene)
            create_scene_request = ManageMoveitSceneRequest()
            create_scene_request.create_scene = True
            create_scene_request.object_mesh_path = self.object_mesh_path
            create_scene_request.object_pose_world = object_pose 
            create_scene_request.mesh_scaling_factor = mesh_scaling_factor
            self.create_scene_response = create_scene_proxy(create_scene_request) 
            #print self.create_scene_response
        except (rospy.ServiceException, e):
            rospy.loginfo('Service create_moveit_scene call failed: %s'%e)
        rospy.loginfo('Service create_moveit_scene is executed %s.' %str(self.create_scene_response))


    def segment_object_client(self, align_obj_frame=True):  
        '''
        segment object out from the image and assign to self.object_segment_response
        '''
        rospy.loginfo('Waiting for service object_segmenter.')
        rospy.wait_for_service('object_segmenter')
        rospy.loginfo('Calling service object_segmenter.')
        try:
            object_segment_proxy = rospy.ServiceProxy('object_segmenter', SegmentGraspObject)
            object_segment_request = SegmentGraspObjectRequest()
            self.object_segment_response = object_segment_proxy(object_segment_request) 
            if align_obj_frame:
                self.object_segment_response.obj = \
                        align_obj.align_object(self.object_segment_response.obj, self.listener)
        except (rospy.ServiceException, e):
            rospy.loginfo('Service object_segmenter call failed: %s'%e)
        rospy.loginfo('Service object_segmenter is executed.')
        if not self.object_segment_response.object_found:
            rospy.logerr('No object found from segmentation!')
            return False
        return True


    def get_visual_data_client(self):
        rospy.loginfo('Waiting for service get_visual_data.')
        rospy.wait_for_service('get_visual_data')
        rospy.loginfo('Calling service get_visual_data.')
        try:
            get_visual_data_proxy = rospy.ServiceProxy('get_visual_data', GetVisualData)
            get_visual_data_request = GetVisualDataRequest()
            get_visual_data_response = get_visual_data_proxy(get_visual_data_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service get_visual_data call failed: %s'%e)
        rospy.loginfo('Service get_visual_data is executed.')
        return get_visual_data_response


    def gen_grasp_preshape_client(self, object_point_cloud, non_random = False, others = None):
        # non-random: robot grasp location is constant; others: other stuff to send to server
        rospy.loginfo('Waiting for service gen_grasp_preshape.')
        rospy.wait_for_service('gen_grasp_preshape')
        rospy.loginfo('Calling service gen_grasp_preshape.')
        try:
            preshape_proxy = rospy.ServiceProxy('gen_grasp_preshape', GraspPreshape)
            preshape_request = GraspPreshapeRequest()
            preshape_request.obj = object_point_cloud
            preshape_request.non_random = non_random
            if others is not None:
                preshape_request.others = others


            self.preshape_response = preshape_proxy(preshape_request) 
        except (rospy.ServiceException):
            rospy.loginfo('Service gen_grasp_preshape call failed:')
        rospy.loginfo('Service gen_grasp_preshape is executed.')
        return self.preshape_response
   

    def control_allegro_config_client(self, go_home=False, close_hand=False, grasp_preshape_idx=-1):
        rospy.loginfo('Waiting for service control_allegro_config.')
        rospy.wait_for_service('control_allegro_config')
        rospy.loginfo('Calling service control_allegro_config.')
        try:
            control_proxy = rospy.ServiceProxy('control_allegro_config', AllegroConfig)
            control_request = AllegroConfigRequest()
            if go_home:
                control_request.go_home = True
            elif close_hand:
                control_request.close_hand = True
            else:
                control_request.allegro_target_joint_state = self.preshape_response.allegro_joint_state[grasp_preshape_idx] 
            self.control_response = control_proxy(control_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service control_allegro_config call failed: %s'%e)
        rospy.loginfo('Service control_allegro_config is executed %s.'%str(self.control_response))




    def listen_mount_pose(self, palm_type, grasp_preshape_idx):
        #bao123 don't know
        self.mount_desired_world = None
        palm_goal_tf_name = palm_type + '_' + str(grasp_preshape_idx)
        rate = rospy.Rate(100.0)
        i = 0
        while (not rospy.is_shutdown()) and i < 10:
            try:
                if self.mount_desired_world is None:
                    (trans_m_to_p, rot_m_to_p) = self.listener.lookupTransform('palm_link', 'reflex_mount', rospy.Time(0))
                    mount_desired_pose = PoseStamped()
                    mount_desired_pose.header.frame_id = palm_goal_tf_name 
                    mount_desired_pose.pose.position.x = trans_m_to_p[0]
                    mount_desired_pose.pose.position.y = trans_m_to_p[1]
                    mount_desired_pose.pose.position.z = trans_m_to_p[2]
                    mount_desired_pose.pose.orientation.x = rot_m_to_p[0]
                    mount_desired_pose.pose.orientation.y = rot_m_to_p[1]
                    mount_desired_pose.pose.orientation.z = rot_m_to_p[2]
                    mount_desired_pose.pose.orientation.w = rot_m_to_p[3]
                    self.mount_desired_world = self.listener.transformPose('world', mount_desired_pose)
                    
                    #Make the pose hight above a threshold to avoid collision
                    #if self.mount_desired_world.pose.position.z < 0.05: 
                    #    rospy.loginfo('Increase height.')
                    #    #raw_input('Increase height')
                    #    self.mount_desired_world.pose.position.z = 0.05

            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                continue
            i += 1
            rate.sleep()


    def listen_palm_obj_pose(self, palm_type, grasp_preshape_idx):
        '''
            Get the palm and object pose for data recording.
        '''
        self.palm_desired_world = None
        #if grasp_preshape_idx == len(self.preshape_response.allegro_joint_state) - 1:
        #    self.object_world_seg_pose = None
        self.object_world_seg_pose = None
        palm_goal_tf_name = palm_type + '_' + str(grasp_preshape_idx)

        rate = rospy.Rate(10.0)
        i = 0
        while (not rospy.is_shutdown()) and i < 10:
            try:
                if self.palm_desired_world is None:
                    (trans_p_to_w, rot_p_to_w) = self.listener.lookupTransform('world', palm_goal_tf_name, rospy.Time(0))
                    palm_desired_world = PoseStamped()
                    palm_desired_world.header.frame_id = 'world'
                    palm_desired_world.pose.position.x = trans_p_to_w[0]
                    palm_desired_world.pose.position.y = trans_p_to_w[1]
                    palm_desired_world.pose.position.z = trans_p_to_w[2]
                    palm_desired_world.pose.orientation.x = rot_p_to_w[0]
                    palm_desired_world.pose.orientation.y = rot_p_to_w[1]
                    palm_desired_world.pose.orientation.z = rot_p_to_w[2]
                    palm_desired_world.pose.orientation.w = rot_p_to_w[3]
    
                    self.palm_desired_world = palm_desired_world

                if self.object_world_seg_pose is None:
                    (trans_o_to_w, rot_o_to_w) = self.listener.lookupTransform('world', 'object_pose', rospy.Time(0))
                    object_world_seg_pose = PoseStamped()
                    object_world_seg_pose.header.frame_id = 'world'
                    object_world_seg_pose.pose.position.x = trans_o_to_w[0]
                    object_world_seg_pose.pose.position.y = trans_o_to_w[1]
                    object_world_seg_pose.pose.position.z = trans_o_to_w[2]
                    object_world_seg_pose.pose.orientation.x = rot_o_to_w[0]
                    object_world_seg_pose.pose.orientation.y = rot_o_to_w[1]
                    object_world_seg_pose.pose.orientation.z = rot_o_to_w[2]
                    object_world_seg_pose.pose.orientation.w = rot_o_to_w[3]

                    self.object_world_seg_pose = object_world_seg_pose
          
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                continue
            i += 1
            rate.sleep()


    def listen_true_palm_pose(self):
        '''
        Listen to get the true palm pose in camera and world frame. 
        This is necesssary because 
        '''
        true_palm_pose_world = None

        rate = rospy.Rate(10.0)
        i = 0
        while (not rospy.is_shutdown()) and i < 10:
            try:
                if true_palm_pose_world is None:
                    (trans_p_to_w, rot_p_to_w) = self.listener.lookupTransform('world', 'palm_link', rospy.Time(0))
                    true_palm_pose_world = PoseStamped()
                    true_palm_pose_world.header.frame_id = 'world'
                    true_palm_pose_world.pose.position.x = trans_p_to_w[0]
                    true_palm_pose_world.pose.position.y = trans_p_to_w[1]
                    true_palm_pose_world.pose.position.z = trans_p_to_w[2]
                    true_palm_pose_world.pose.orientation.x = rot_p_to_w[0]
                    true_palm_pose_world.pose.orientation.y = rot_p_to_w[1]
                    true_palm_pose_world.pose.orientation.z = rot_p_to_w[2]
                    true_palm_pose_world.pose.orientation.w = rot_p_to_w[3]

            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                continue
            i += 1
            rate.sleep()

        return true_palm_pose_world 


    
    def arm_moveit_planner_client(self, go_home=False, current_position=None, joint_goal=None, cartesian_goal=None):
        #bao123 
        '''
        return Is there any plan?
        calculate plan and assign to self.planning_response
        '''
        
        rospy.loginfo('Waiting for service moveit_cartesian_pose_planner.')
        rospy.wait_for_service('moveit_cartesian_pose_planner')
        rospy.loginfo('Calling service moveit_cartesian_pose_planner.')
        try:
            planning_proxy = rospy.ServiceProxy('moveit_cartesian_pose_planner', PalmGoalPoseWorld)
            planning_request = PalmGoalPoseWorldRequest()       
            planning_request.current_joint_states = current_position
            if go_home:
                planning_request.go_home = True
            elif joint_goal is not None:
                planning_request.go_to_joint_goal = True
                planning_request.joint_goal = joint_goal    # 8 first joints        
            elif cartesian_goal is not None:
                planning_request.palm_goal_pose_world = cartesian_goal
            else:
                rospy.loginfo('Missing joint goal/ cartesian goal')
            self.planning_response = planning_proxy(planning_request) 
        except (rospy.ServiceException):
            rospy.loginfo('Service moveit_cartesian_pose_planner call failed')
        rospy.loginfo('Service moveit_cartesian_pose_planner is executed %s.'
                %str(self.planning_response.success))
        
        plan_list = []
        if not self.planning_response.success:
            rospy.loginfo('Does not have a plan to execute!')
        else:        
            for point in self.planning_response.plan_traj.points:
                plan_list.append(list(point.positions))
        return plan_list


    def arm_movement_client(self):
        rospy.loginfo('Waiting for service arm_movement.')
        rospy.wait_for_service('arm_movement')
        rospy.loginfo('Calling service arm_movement.')
        try:
            movement_proxy = rospy.ServiceProxy('arm_movement', MoveArm)
            movement_request = MoveArmRequest()
            self.movement_response = movement_proxy(movement_request) 
            #print self.movement_response
        except (rospy.ServiceException, e):
            rospy.loginfo('Service arm_movement call failed: %s'%e)
        rospy.loginfo('Service arm_movement is executed %s.'%str(self.movement_response))


    def execute_arm_plan(self, send_cmd_manually=False):
        '''
        execute plan in self.planning_response.plan_traj
        return false if no plan
        '''
        #bao123
        rospy.loginfo('Executing moveit arm plan...')
        if not self.planning_response.success:
            rospy.loginfo('Does not have a plan to execute!')
            return False
        plan_traj = self.planning_response.plan_traj

        # if self.palm_planner is None:
        #     self.palm_planner = PalmPosePlanner(init_node=False)
        # 
        # if self.smooth_plan_traj:
        #     #Smooth trajectory.
        #     smooth_success, smooth_traj = \
        #             self.palm_planner.robot.get_smooth_traj(plan_traj)
        #     rospy.loginfo('Trajectory smoothing success: %s.'%str(smooth_success))
        #     if smooth_success:
        #         plan_traj = smooth_traj

        # if send_cmd_manually:
        #     self.palm_planner.plot_traj(plan_traj)

        #     # send to robot:
        #     send_cmd = raw_input('send to robot? (y/n)')
        #     if(send_cmd == 'y'):
        #         self.palm_planner.robot.send_jtraj(plan_traj)
        #         #raw_input('Hit any key to keep going after the robot control is done!')
        #         #TO DO: make sure the robot finishing executing the trajectory before returning.
        #         return True
        #     return False
        # else:
        #     # send to robot:
        #     self.palm_planner.robot.send_jtraj(plan_traj)
        #     return True

        if self.robot_traj_manager is None:
            self.robot_traj_manager = robot_traj_interface.robotTrajInterface(init_node=False)
        
        if self.smooth_plan_traj:
            #Smooth trajectory.
            smooth_success, smooth_traj = \
                    self.robot_traj_manager.get_smooth_traj(plan_traj)
            rospy.loginfo('Trajectory smoothing success: %s.'%str(smooth_success))
            if smooth_success:
                plan_traj = smooth_traj

        if send_cmd_manually:
            plot_traj.plot_j_acc_profile(plan_traj, 7)

            # send to robot:
            send_cmd = raw_input('send to robot? (y/n)')
            if(send_cmd == 'y'):
                self.robot_traj_manager.send_jtraj(plan_traj)
                #raw_input('Hit any key to keep going after the robot control is done!')
                #TO DO: make sure the robot finishing executing the trajectory before returning.
                return True
            return False
        else:
            # send to robot (run robot according to the trajectory planned by moveit):
            self.robot_traj_manager.send_jtraj(plan_traj)
            return True


    def grasp_client(self, top_grasp):
        rospy.loginfo('Waiting for service grasp.')
        rospy.wait_for_service('grasp')
        rospy.loginfo('Calling service grasp.')
        try:
            grasp_proxy = rospy.ServiceProxy('grasp', GraspAllegro)
            grasp_request = GraspAllegroRequest()
            grasp_request.joint_vel_thresh = self.joint_vel_thresh 
            grasp_request.top_grasp = top_grasp
            self.grasp_response = grasp_proxy(grasp_request) 
            #print self.grasp_response
        except (rospy.ServiceException, e):
            rospy.loginfo('Service grasp call failed: %s'%e)
        rospy.loginfo('Service grasp is executed %s.'%self.grasp_response.success)


    def grasp_control_client(self, grasp_type='prec', non_thumb_speed=0.2, thumb_speed=0.25):
    #def grasp_control_client(self, grasp_type='prec', non_thumb_speed=0.1, thumb_speed=0.15):
        rospy.loginfo('Waiting for service grasp_control.')
        rospy.wait_for_service('grasp_control')
        rospy.loginfo('Calling service grasp_control.')
        try:
            grasp_proxy = rospy.ServiceProxy('grasp_control', PreshapeControl)
            grasp_request = PreshapeControlRequest()
            grasp_request.joint_vel_thresh = .0001 #.01 
            grasp_request.grasp_type = grasp_type
            grasp_request.close_non_thumb_speed = non_thumb_speed
            grasp_request.close_thumb_speed = thumb_speed
            self.grasp_response = grasp_proxy(grasp_request) 
            #print self.grasp_response
        except (rospy.ServiceException, e):
            rospy.loginfo('Service grasp call failed: %s'%e)
        rospy.loginfo('Service grasp is executed %s.'%self.grasp_response.success)


    def clean_moveit_scene_client(self):
        rospy.loginfo('Waiting for service clean_moveit_scene.')
        rospy.wait_for_service('clean_moveit_scene')
        rospy.loginfo('Calling service clean_moveit_scene.')
        try:
            clean_scene_proxy = rospy.ServiceProxy('clean_moveit_scene', ManageMoveitScene)
            clean_scene_request = ManageMoveitSceneRequest()
            clean_scene_request.clean_scene = True
            self.clean_scene_response = clean_scene_proxy(clean_scene_request) 
            #print self.clean_scene_response
        except (rospy.ServiceException, e):
            rospy.loginfo('Service clean_moveit_scene call failed: %s'%e)
        rospy.loginfo('Service clean_moveit_scene is executed %s.' %str(self.clean_scene_response))


    def get_object_pose_from_gazebo(self, gz_model_msg):
        #rostopic echo /gazebo/model_states
        self.object_name_gazebo = gz_model_msg.name[-1]
        self.object_pose_gazebo = gz_model_msg.pose[-1]


    def get_grasp_label(self):
        if self.object_name_gazebo != self.object_name:
            rospy.logerr('Got wrong objects from Gazebo!')
        grasp_success = 0
        if self.object_pose_gazebo.position.z >= self.grasp_success_object_height:
            grasp_success = 1
        return grasp_success


    def lift_arm_movement_client(self):
        rospy.loginfo('Waiting for service arm_movement to lift.')
        rospy.wait_for_service('arm_movement')
        rospy.loginfo('Calling service arm_movement to lift.')
        try:
            movement_proxy = rospy.ServiceProxy('arm_movement', MoveArm)
            movement_request = MoveArmRequest()
            self.movement_response = movement_proxy(movement_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service arm_movement call to lift failed: %s'%e)
        rospy.loginfo('Service arm_movement to lift is executed %s.'%str(self.movement_response.success))


    def lift_moveit_planner_client(self, current_position, height_to_lift=None):
        if height_to_lift == None:
            height_to_lift = self.lift_height
        rospy.loginfo('Waiting for service moveit_cartesian_pose_planner to lift.')
        rospy.wait_for_service('moveit_cartesian_pose_planner')
        rospy.loginfo('Calling service moveit_cartesian_pose_planner to lift.')
        try:
            planning_proxy = rospy.ServiceProxy('moveit_cartesian_pose_planner', PalmGoalPoseWorld)
            planning_request = PalmGoalPoseWorldRequest()
            planning_request.current_joint_states = current_position
            planning_request.palm_goal_pose_world = copy.deepcopy(self.close_palm_pose_world.pose)
            planning_request.palm_goal_pose_world.position.z += height_to_lift -  ROBOT_Z_OFFSET
            self.planning_response = planning_proxy(planning_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service moveit_cartesian_pose_planner call to lift failed: %s'%e)
        rospy.loginfo('Service moveit_cartesian_pose_planner to lift is executed %s.'%str(self.planning_response.success))

        plan_list = []  # Convert ROS Trajectory msg to a list for easy usage
        if not self.planning_response.success:
            rospy.loginfo('Does not have a plan to execute!')
        else:        
            for point in self.planning_response.plan_traj.points:
                plan_list.append(list(point.positions))
        return plan_list #If no plan, plan_list is empty

    




    def lift_task_vel_planner_client(self, height_to_lift=0.15):
        rospy.loginfo('Waiting for service straight_line_planner to lift.')
        rospy.wait_for_service('straight_line_planner')
        rospy.loginfo('Calling service straight_line_planner to lift.')
        try:
            planning_proxy = rospy.ServiceProxy('straight_line_planner', StraightLinePlan)
            planning_request = StraightLinePlanRequest()
            planning_request.lift_height = height_to_lift
            self.planning_response = planning_proxy(planning_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service straight_line_planner call to lift failed: %s'%e)
        rospy.loginfo('Service straight_line_planner to lift is executed %s.'
                      %str(self.planning_response.success))
        return self.planning_response.success


    def place_arm_movement_client(self):
        rospy.loginfo('Move the arm to palce the object back.')
        place_x_loc = np.random.uniform(self.place_x_min, self.place_x_max) 
        place_y_loc = np.random.uniform(self.place_y_min, self.place_y_max) 
        place_pose = copy.deepcopy(self.mount_desired_world.pose)
        place_pose.position.x = place_x_loc
        place_pose.position.y = place_y_loc
        #print 'place_pose:', place_pose
        dc_client.arm_moveit_planner_client(place_goal_pose=place_pose)
        dc_client.arm_movement_client()


    def place_control_allegro_client(self):
        '''
        Place the object then go home
        '''
        rospy.loginfo('Open the allegro hand to place the object.')
        #self.control_allegro_config_client(go_home=True)
        self.control_reflex_config_client(go_home=True) #TODO: Parameterize

    def move_arm_home_client(self):
        rospy.loginfo('Move the arm to go home.')
        dc_client.arm_moveit_planner_client(go_home=True)
        dc_client.arm_movement_client()


    def move_arm_home(self):
        rospy.loginfo('Move the arm to go home.')
        dc_client.arm_moveit_planner_client(go_home=True)
        dc_client.execute_arm_plan()


    def record_grasp_visual_data_client(self, save_visual_data_request):
        rospy.loginfo('Waiting for service save_visual_data.')
        rospy.wait_for_service('save_visual_data')
        rospy.loginfo('Calling service save_visual_data.')
        try:
            save_visual_data_proxy = rospy.ServiceProxy('save_visual_data', SaveVisualData)
            self.save_visual_data_response = save_visual_data_proxy(save_visual_data_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service save_visual_data call failed: %s'%e)
        rospy.loginfo('Service save_visual_data is executed %s.'
                       %self.save_visual_data_response.save_visual_data_success)


    def record_grasp_data_client(self, grasp_preshape_idx=None):
        '''
        depend on whether the grasp is success or not (self.get_grasp_label()), record the RGBD images into the correct folder
        '''
        #bao123
        rospy.loginfo('Waiting for service record_grasp_data.')
        rospy.wait_for_service('record_grasp_data')
        rospy.loginfo('Calling service record_grasp_data.')
        # self.save_grasp_visual_data()
        try:
            record_grasp_data_proxy = rospy.ServiceProxy('record_grasp_data', SimGraspData)
            record_grasp_data_request = SimGraspDataRequest()
            record_grasp_data_request.object_name = self.object_name
            record_grasp_data_request.grasp_id = self.grasp_id
            record_grasp_data_request.time_stamp = time.time()

            
            record_grasp_data_request.grasp_success_label = self.grasp_label 
            record_grasp_data_request.obj_id = self.cur_object_id

            record_grasp_data_request.object_world_seg_pose = self.object_world_seg_pose
            record_grasp_data_request.object_world_sim_pose = self.object_world_sim_pose             
            record_grasp_data_request.preshape_palm_world_pose = self.preshape_response.palm_goal_pose_world[self.top_grasp_preshape_idx]          
            record_grasp_data_request.true_preshape_palm_world_pose = self.true_palm_pose_world
            # record_grasp_data_request.close_shape_palm_world_pose = self.close_palm_pose_world 
            # record_grasp_data_request.lift_shape_palm_world_pose = self.lift_palm_pose_world 
            record_grasp_data_request.top_grasp = True

            self.record_grasp_data_response = record_grasp_data_proxy(record_grasp_data_request) 
            rospy.loginfo('****' + str(self.record_grasp_data_response))
        except (rospy.ServiceException, e):
            rospy.loginfo('Service record_grasp_data call failed: %s'%e)
        rospy.loginfo('Service record_grasp_data is executed %s.'%self.record_grasp_data_response.save_h5_success)

        self.record_grasp_data_request = record_grasp_data_request
        # self.save_lift_visual_data()


    def get_scene_cloud_save_path(self, lift):
        if lift:
            scene_cloud_save_path = self.save_visual_data_pre_path + \
                    'pcd/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                    '_grasp_' + str(self.grasp_id) + '_lift.pcd' 
        else:
            scene_cloud_save_path = self.save_visual_data_pre_path + \
                        'pcd/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                        '_grasp_' + str(self.grasp_id) + '.pcd'          
        return scene_cloud_save_path   

    def get_rgb_image_save_path_with_label(self):

        if self.grasp_label == 1:
            rgb_image_save_path = self.save_visual_data_pre_path + \
                'gazebo_rgb_image/suc_grasps/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                '_grasp_' + str(self.grasp_id) + '.png' 
        else:
            rgb_image_save_path = self.save_visual_data_pre_path + \
                'gazebo_rgb_image/fail_grasps/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                '_grasp_' + str(self.grasp_id) + '.png'     
                       
        return rgb_image_save_path
    
    def get_rgb_image_save_path(self, lift):
        if lift:
            rgb_image_save_path = self.save_visual_data_pre_path + \
                        'rgb_image/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                        '_grasp_' + str(self.grasp_id) + '_lift.png'    
        else:
            rgb_image_save_path = self.save_visual_data_pre_path + \
                        'rgb_image/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                        '_grasp_' + str(self.grasp_id) + '.png'             
        return rgb_image_save_path

    def get_depth_image_save_path(self, lift):
        if lift:
            depth_image_save_path = self.save_visual_data_pre_path + \
                        'depth_image/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                        '_grasp_' + str(self.grasp_id) + '_lift.png'    
        else:
            depth_image_save_path = self.save_visual_data_pre_path + \
                        'depth_image/' + 'object_' + str(self.cur_object_id) + '_' + str(self.object_name) + \
                        '_grasp_' + str(self.grasp_id) + '.png'             
        return depth_image_save_path

    def segment_and_generate_preshape(self):
        '''
        Segment the object out and generate preshape based on the object
        '''
        #bao123
        
        object_found = self.segment_object_client()
        if not object_found:
            return False
        self.grasp_visual_data_response = self.get_visual_data_client()
        self.gen_grasp_preshape_client()
        return True


    def grasp_and_lift_object_steps(self, object_pose, grasp_preshape_idx):
        '''
        grasp + lift object up to see success/fail
        grasp_preshape_idx: have a bunch of preshape, use which one?
        '''
        self.create_moveit_scene_client(object_pose)

        #self.control_allegro_config_client(grasp_preshape_idx=grasp_preshape_idx)
        self.control_reflex_config_client(grasp_preshape_idx=grasp_preshape_idx) #TODO: Parameterize

        self.listen_mount_pose('heu', grasp_preshape_idx)
        self.listen_palm_obj_pose('heu', grasp_preshape_idx)

        if self.mount_desired_world.pose.position.z < self.min_palm_height:
            rospy.loginfo('###Increase grasp height!')
            palm_rand_height = np.random.uniform(self.min_palm_height, self.max_palm_height)
            self.mount_desired_world.pose.position.z = palm_rand_height
            #self.mount_desired_world.pose.position.z = self.min_palm_height 

        moveit_found_plan = self.arm_moveit_planner_client()
        if not moveit_found_plan:
            return False
        #self.arm_movement_client()
        if not self.execute_arm_plan():
            return False

        self.true_palm_pose_world = self.listen_true_palm_pose()
        self.true_preshape_hand_js = self.true_hand_joint_state

        #self.grasp_control_client()
        self.control_reflex_config_client(close_hand=True)

        self.close_palm_pose_world = self.listen_true_palm_pose()
        self.close_hand_js = self.true_hand_joint_state 

        self.clean_moveit_scene_client()

        #lift = raw_input('Lift or not (y/n)?')
        lift = 'y'
        if lift == 'y':
            task_vel_lift_succes = self.lift_task_vel_planner_client()
            if not task_vel_lift_succes:
                rospy.loginfo('Task velocity straight line planner fails to find a valid plan' \
                               ' for lifting. Switch to the moveit planner.')
                self.lift_moveit_planner_client()
            self.execute_arm_plan(send_cmd_manually=False)
            self.lift_palm_pose_world = self.listen_true_palm_pose()
            self.lift_hand_js = self.true_hand_joint_state 
            self.lift_visual_data_response = self.get_visual_data_client()
            
        return True


    def place_object_steps(self, move_arm=True):
        '''
        Place object 
        '''
        #bao123
        self.place_control_allegro_client()
        if move_arm:
            self.move_arm_home()


    def update_gazebo_object_client(self, object_name, object_pose_array, object_model_name):
        '''
            Gazebo management client to send request to create one new object and delete the 
            previous object.
        '''
        rospy.loginfo('Waiting for service update_gazebo_object.')
        rospy.wait_for_service('update_gazebo_object')
        rospy.loginfo('Calling service update_gazebo_object.')
        try:
            update_object_gazebo_proxy = rospy.ServiceProxy('update_gazebo_object', UpdateObjectGazebo)
            update_object_gazebo_request = UpdateObjectGazeboRequest()
            update_object_gazebo_request.object_name = object_name
            update_object_gazebo_request.object_pose_array = object_pose_array
            update_object_gazebo_request.object_model_name = object_model_name
            update_object_gazebo_response = update_object_gazebo_proxy(update_object_gazebo_request) 
        except (rospy.ServiceException, e):
            rospy.loginfo('Service update_gazebo_object call failed: %s'%e)
        rospy.loginfo('Service update_gazebo_object is executed %s.'%str(update_object_gazebo_response))
        return update_object_gazebo_response.success


    def seg_obj_from_file_client(self, pcd_file_path, align_obj_frame=True):
        rospy.loginfo('Waiting for service object_segmenter.')
        rospy.wait_for_service('object_segmenter')
        rospy.loginfo('Calling service object_segmenter.')
        try:
            object_segment_proxy = rospy.ServiceProxy('object_segmenter', 
                                                        SegmentGraspObject)
            object_segment_request = SegmentGraspObjectRequest()
            object_segment_request.client_cloud_path = pcd_file_path
            object_segment_response = object_segment_proxy(object_segment_request) 
            if align_obj_frame:
                object_segment_response.obj = \
                        align_obj.align_object(object_segment_response.obj)
        except (rospy.ServiceException):
            rospy.loginfo('Service object_segmenter call failed: %s')
        rospy.loginfo('Service object_segmenter is executed.')
        if not object_segment_response.object_found:
            rospy.logerr('No object found from segmentation!')
            # return None
        return object_segment_response


    def get_pose_stamped_from_array(self, pose_array, frame_id='/world'):
        pose_stamped = PoseStamped()
        pose_stamped.header.frame_id = frame_id 
        # RPY to quaternion
        r = R.from_euler('xyz', [pose_array[0], pose_array[1], pose_array[2]])
        # r = R.from_euler('zyx', [pose_array[2], pose_array[1], pose_array[0]])
        pose_quaternion = r.as_quat()
        pose_stamped.pose.orientation.x, pose_stamped.pose.orientation.y, \
                pose_stamped.pose.orientation.z, pose_stamped.pose.orientation.w = pose_quaternion
        pose_stamped.pose.position.x, pose_stamped.pose.position.y, pose_stamped.pose.position.z = \
                pose_array[3:]
        return pose_stamped


    def gen_object_pose(self):
        #bao123 generate random object pose
        self.object_world_sim_pose = None
        place_x_loc = np.random.uniform(self.place_x_min, self.place_x_max) 
        place_y_loc = np.random.uniform(self.place_y_min, self.place_y_max) 
        x_orientation = 1/2 * np.pi
        z_orientation = np.random.uniform(0., 2 * np.pi)  #-1/2 * np.pi 
        object_pose = [x_orientation, 0. ,z_orientation, place_x_loc, place_y_loc, self.table_len_z]
        # rospy.loginfo('y orientation: ' + str(y_orientation*180/np.pi))
        rospy.loginfo('Generated random object pose:')
        rospy.loginfo(object_pose)
        object_pose_stamped = self.get_pose_stamped_from_array(object_pose) 
        # rospy.loginfo('object_pose_stamped:')
        # rospy.loginfo(object_pose_stamped)
        self.object_world_sim_pose = object_pose_stamped
        return object_pose_stamped


