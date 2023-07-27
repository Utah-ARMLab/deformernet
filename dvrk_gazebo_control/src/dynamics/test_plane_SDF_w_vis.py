#!/usr/bin/env python3
from __future__ import print_function, division, absolute_import


import sys
import roslib.packages as rp
pkg_path = rp.get_pkg_dir('dvrk_gazebo_control')
sys.path.append(pkg_path + '/src')
import os
import math
import numpy as np
from isaacgym import gymapi
from isaacgym import gymtorch
from isaacgym import gymutil
from copy import copy, deepcopy
import rospy
# from dvrk_gazebo_control.srv import *
from geometry_msgs.msg import PoseStamped, Pose
from GraspDataCollectionClient import GraspDataCollectionClient
import open3d
from utils import open3d_ros_helper as orh
from utils import o3dpc_to_GraspObject_msg as o3dpc_GO
#import pptk
from utils.isaac_utils import isaac_format_pose_to_PoseStamped as to_PoseStamped
from utils.isaac_utils import fix_object_frame
# from utils.record_data_h5 import RecordGraspData_sparse
import pickle
# from ShapeServo import *
# from sklearn.decomposition import PCA
import timeit
from copy import deepcopy
from PIL import Image
from sklearn.neighbors import NearestNeighbors

from core import Robot
from behaviors import MoveToPose, TaskVelocityControl

# sys.path.append('/home/baothach/shape_servo_DNN/generalization_tasks')

# # from pointcloud_recon_2 import PointNetShapeServo, PointNetShapeServo2
# from architecture import DeformerNet2
import torch

sys.path.append("/home/baothach/shape_servo_DNN/dynamics_SDF/")
from architecture import TransitionSDF, PlaneSDF

sys.path.append("/home/baothach/ll4ma_3drecon_uois/dependencies/PointSDFPytorch/")
from models.sdf_pointconv_model import *
from models.sdf_pointconv_model import PointConvModel as PointSDFModel
from data_generation.object_cloud import process_object_cloud
from trimesh.transformations import transform_points



ROBOT_Z_OFFSET = 0.25
# angle_kuka_2 = -0.4
# init_kuka_2 = 0.15
two_robot_offset = 0.86

sys.path.append('/home/baothach/shape_servo_DNN')
from farthest_point_sampling import *

def init():
    for i in range(num_envs):
        # # Kuka 2
        davinci_dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_NONE)
        davinci_dof_states['pos'][8] = 1.5
        davinci_dof_states['pos'][9] = 0.8
        gym.set_actor_dof_states(envs[i], kuka_handles_2[i], davinci_dof_states, gymapi.STATE_POS)

def get_point_cloud():
    gym.refresh_particle_state_tensor(sim)
    particle_state_tensor = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim)))
    point_cloud = particle_state_tensor.numpy()[:, :3]  
    
    # pcd = open3d.geometry.PointCloud()
    # pcd.points = open3d.utility.Vector3dVector(np.array(point_cloud))
    # open3d.visualization.draw_geometries([pcd])     
    # return list(point_cloud)
    return point_cloud.astype('float32')

def modify_pc(point_cloud):
    dict = process_object_cloud(point_cloud)
    point_cloud = dict['object_cloud']
    object_transform = dict['object_transform']
    object_scale = dict['scale']    
    point_cloud = transform_points(point_cloud, object_transform, translate=True)
    point_cloud = point_cloud * object_scale

    return point_cloud

def passed_plane(plane, point_cloud):
    failed_points = np.array([p for p in point_cloud if plane[0]*p[0] + plane[1]*p[1] + plane[2]*p[2] > -plane[3]]) 
    total_points = point_cloud.shape[0]
    percent_passed = 1 - len(failed_points)/total_points    
    return percent_passed

def get_partial_point_cloud(i):

    # Render all of the image sensors only when we need their output here
    # rather than every frame.
    gym.render_all_camera_sensors(sim)

    points = []
    print("Converting Depth images to point clouds. Have patience...")
    # for c in range(len(cam_handles)):
    
    # print("Deprojecting from camera %d, %d" % i))
    # Retrieve depth and segmentation buffer
    depth_buffer = gym.get_camera_image(sim, envs_obj[i], cam_handles[i], gymapi.IMAGE_DEPTH)
    seg_buffer = gym.get_camera_image(sim, envs_obj[i], cam_handles[i], gymapi.IMAGE_SEGMENTATION)


    # Get the camera view matrix and invert it to transform points from camera to world
    # space
    
    vinv = np.linalg.inv(np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], cam_handles[0])))

    # Get the camera projection matrix and get the necessary scaling
    # coefficients for deprojection
    proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    fu = 2/proj[0, 0]
    fv = 2/proj[1, 1]

    # Ignore any points which originate from ground plane or empty space
    # depth_buffer[seg_buffer == 11] = -10001

    centerU = cam_width/2
    centerV = cam_height/2
    for k in range(cam_width):
        for t in range(cam_height):
            if depth_buffer[t, k] < -3:
                continue

            u = -(k-centerU)/(cam_width)  # image-space coordinate
            v = (t-centerV)/(cam_height)  # image-space coordinate
            d = depth_buffer[t, k]  # depth buffer value
            X2 = [d*fu*u, d*fv*v, d, 1]  # deprojection vector
            p2 = X2*vinv  # Inverse camera view to get world coordinates
            # print("p2:", p2)
            if p2[0, 2] > 0.01:
                points.append([p2[0, 0], p2[0, 1], p2[0, 2]])

    # pcd = open3d.geometry.PointCloud()
    # pcd.points = open3d.utility.Vector3dVector(np.array(points))
    # open3d.visualization.draw_geometries([pcd]) 

    # return points
    return np.array(points).astype('float32')

def visualize_plane(plane_eq, x_range=[-0.3,0.3], y_range=0.5, z_range=0.10,num_pts = 10000):
    plane = []
    for i in range(num_pts):
        x = np.random.uniform(x_range[0], x_range[1])
        z = np.random.uniform(0.0, z_range)
        y = -(plane_eq[0]*x + plane_eq[2]*z + plane_eq[3])/plane_eq[1]
        if -y_range < y < 0:
            plane.append([x, y, z])     
    return plane

def get_goal_projected_on_image(goal_pc, i, thickness = 0):
    # proj = gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[i])
    # fu = 2/proj[0, 0]
    # fv = 2/proj[1, 1]
    

    u_s =[]
    v_s = []
    for point in goal_pc:
        point = list(point) + [1]

        point = np.expand_dims(np.array(point), axis=0)

        point_cam_frame = point * np.matrix(gym.get_camera_view_matrix(sim, envs_obj[i], vis_cam_handles[0]))
        # print("point_cam_frame:", point_cam_frame)
        # image_coordinates = (gym.get_camera_proj_matrix(sim, envs_obj[i], cam_handles[0]) * point_cam_frame)
        # print("image_coordinates:",image_coordinates)
        # u_s.append(image_coordinates[1, 0]/image_coordinates[2, 0]*2)
        # v_s.append(image_coordinates[0, 0]/image_coordinates[2, 0]*2)
        # print("fu fv:", fu, fv)
        u_s.append(1/2 * point_cam_frame[0, 0]/point_cam_frame[0, 2])
        v_s.append(1/2 * point_cam_frame[0, 1]/point_cam_frame[0, 2])      
          
    centerU = vis_cam_width/2
    centerV = vis_cam_height/2    
    # print(centerU - np.array(u_s)*cam_width)
    # y_s = (np.array(u_s)*cam_width).astype(int)
    # x_s = (np.array(v_s)*cam_height).astype(int)
    y_s = (centerU - np.array(u_s)*vis_cam_width).astype(int)
    x_s = (centerV + np.array(v_s)*vis_cam_height).astype(int)    

    if thickness != 0:
        new_y_s = deepcopy(list(y_s))
        new_x_s = deepcopy(list(x_s))
        for y, x in zip(y_s, x_s):
            for t in range(1, thickness+1):
                new_y_s.append(max(y-t,0))
                new_x_s.append(max(x-t,0))
                new_y_s.append(max(y-t,0))
                new_x_s.append(min(x+t, vis_cam_height-1))                
                new_y_s.append(min(y+t, vis_cam_width-1))
                new_x_s.append(max(x-t,0))                    
                new_y_s.append(min(y+t, vis_cam_width-1))                
                new_x_s.append(min(x+t, vis_cam_height-1))
        y_s = new_y_s
        x_s = new_x_s
    # print(x_s)
    return x_s, y_s

if __name__ == "__main__":

    # initialize gym
    gym = gymapi.acquire_gym()

    # parse arguments
    args = gymutil.parse_arguments(
        description="Kuka Bin Test",
        custom_parameters=[
            {"name": "--num_envs", "type": int, "default": 1, "help": "Number of environments to create"},
            {"name": "--num_objects", "type": int, "default": 10, "help": "Number of objects in the bin"},
            {"name": "--obj_name", "type": str, "default": 'box_0', "help": "select variations of a primitive shape"},
            {"name": "--headless", "type": bool, "default": False, "help": "headless mode"}])

    num_envs = args.num_envs
    


    # configure sim
    sim_type = gymapi.SIM_FLEX
    sim_params = gymapi.SimParams()
    sim_params.up_axis = gymapi.UP_AXIS_Z
    sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)
    if sim_type is gymapi.SIM_FLEX:
        sim_params.substeps = 4
        # print("=================sim_params.dt:", sim_params.dt)
        sim_params.dt = 1./60.
        sim_params.flex.solver_type = 5
        sim_params.flex.num_outer_iterations = 4
        sim_params.flex.num_inner_iterations = 50
        sim_params.flex.relaxation = 0.7
        sim_params.flex.warm_start = 0.1
        sim_params.flex.shape_collision_distance = 5e-4
        sim_params.flex.contact_regularization = 1.0e-6
        sim_params.flex.shape_collision_margin = 1.0e-4
        sim_params.flex.deterministic_mode = True

    sim = gym.create_sim(args.compute_device_id, args.graphics_device_id, sim_type, sim_params)
    print("==========args.compute_device_id:", args.compute_device_id)
    # # Get primitive shape dictionary to know the dimension of the object   
    # object_meshes_path = "/home/baothach/sim_data/Custom/Custom_mesh/multi_boxes_5kPa"    
    # with open(os.path.join(object_meshes_path, "primitive_dict_box.pickle"), 'rb') as handle:
    #     data = pickle.load(handle)    
    # h = data[args.obj_name]["height"]
    # w = data[args.obj_name]["width"]
    # thickness = data[args.obj_name]["thickness"]

    # add ground plane
    plane_params = gymapi.PlaneParams()
    plane_params.normal = gymapi.Vec3(0, 0, 1) # z-up ground
    gym.add_ground(sim, plane_params)

    # create viewer
    if not args.headless:
        viewer = gym.create_viewer(sim, gymapi.CameraProperties())
        if viewer is None:
            print("*** Failed to create viewer")
            quit()

    # load robot assets
    pose_2 = gymapi.Transform()
    pose_2.p = gymapi.Vec3(0.0, 0.0, ROBOT_Z_OFFSET)
    # pose_2.p = gymapi.Vec3(0.0, 0.85, ROBOT_Z_OFFSET)
    pose_2.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

    asset_options = gymapi.AssetOptions()
    asset_options.armature = 0.001
    asset_options.fix_base_link = True
    asset_options.thickness = 0.0001


    asset_root = "./src/dvrk_env"
    kuka_asset_file = "dvrk_description/psm/psm_for_issacgym.urdf"


    asset_options.fix_base_link = True
    asset_options.flip_visual_attachments = False
    asset_options.collapse_fixed_joints = True
    asset_options.disable_gravity = True
    asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS

    if sim_type is gymapi.SIM_FLEX:
        asset_options.max_angular_velocity = 40000.

    print("Loading asset '%s' from '%s'" % (kuka_asset_file, asset_root))
    kuka_asset = gym.load_asset(sim, asset_root, kuka_asset_file, asset_options)

    asset_root = "/home/baothach/sim_data/Custom/Custom_urdf"
    # soft_asset_file = "cheez_it_white_cheddar.urdf"
    soft_asset_file = "box.urdf"

    soft_pose = gymapi.Transform()
    soft_pose.p = gymapi.Vec3(0.0, -0.42, 0.01818)
    soft_pose.r = gymapi.Quat(0.0, 0.0, 0.707107, 0.707107)
    soft_thickness = 0.0005#0.0005    # important to add some thickness to the soft body to avoid interpenetrations






    asset_options = gymapi.AssetOptions()
    asset_options.fix_base_link = True
    asset_options.thickness = soft_thickness
    asset_options.disable_gravity = True

    soft_asset = gym.load_asset(sim, asset_root, soft_asset_file, asset_options)

        
    
 
    
    # set up the env grid
    # spacing = 0.75
    spacing = 0.0
    env_lower = gymapi.Vec3(-spacing, 0.0, -spacing)
    env_upper = gymapi.Vec3(spacing, spacing, spacing)
  

    # cache some common handles for later use
    envs = []
    envs_obj = []
    kuka_handles = []
    kuka_handles_2 = []
    object_handles = []
    

    print("Creating %d environments" % num_envs)
    num_per_row = int(math.sqrt(num_envs))
    base_poses = []

    for i in range(num_envs):
        # create env
        env = gym.create_env(sim, env_lower, env_upper, num_per_row)
        envs.append(env)

        # add kuka2
        kuka_2_handle = gym.create_actor(env, kuka_asset, pose_2, "kuka2", i, 1, segmentationId=11)        
        

        # add soft obj        
        env_obj = env
        # env_obj = gym.create_env(sim, env_lower, env_upper, num_per_row)
        envs_obj.append(env_obj)        
        
        soft_actor = gym.create_actor(env_obj, soft_asset, soft_pose, "soft", i, 0)
        object_handles.append(soft_actor)


        kuka_handles_2.append(kuka_2_handle)



    dof_props_2 = gym.get_asset_dof_properties(kuka_asset)
    dof_props_2["driveMode"].fill(gymapi.DOF_MODE_POS)
    dof_props_2["stiffness"].fill(200.0)
    dof_props_2["damping"].fill(40.0)
    dof_props_2["stiffness"][8:].fill(1)
    dof_props_2["damping"][8:].fill(2)  
    vel_limits = dof_props_2['velocity']    

    # Camera setup
    if not args.headless:
        cam_pos = gymapi.Vec3(1, 0.5, 1)
        # cam_pos = gymapi.Vec3(0.3, -0.7, 0.3)
        # cam_pos = gymapi.Vec3(0.3, -0.1, 0.5)  # final setup for thin layer tissue
        # cam_pos = gymapi.Vec3(0.5, -0.36, 0.3)
        # cam_target = gymapi.Vec3(0.0, 0.0, 0.1)
        cam_target = gymapi.Vec3(0.0, -0.36, 0.1)
        middle_env = envs[num_envs // 2 + num_per_row // 2]
        gym.viewer_camera_look_at(viewer, middle_env, cam_pos, cam_target)

    # Camera for point cloud setup
    cam_positions = []
    cam_targets = []
    cam_handles = []
    cam_width = 256
    cam_height = 256
    cam_props = gymapi.CameraProperties()
    cam_props.width = cam_width
    cam_props.height = cam_height
    # cam_positions.append(gymapi.Vec3(0.12, -0.55, 0.15))
    cam_positions.append(gymapi.Vec3(0.1, -0.5, 0.2))
    cam_targets.append(gymapi.Vec3(0.0, -0.45, 0.00))
    for i, env_obj in enumerate(envs_obj):
            cam_handles.append(gym.create_camera_sensor(env_obj, cam_props))
            gym.set_camera_location(cam_handles[i], env_obj, cam_positions[0], cam_targets[0])
    print("============cam_handle:", cam_handles[0])  
    
    # visualization camera
    vis_cam_positions = []
    vis_cam_targets = []
    vis_cam_handles = []
    vis_cam_width = 400
    vis_cam_height = 400
    vis_cam_props = gymapi.CameraProperties()
    vis_cam_props.width = vis_cam_width
    vis_cam_props.height = vis_cam_height



    vis_cam_positions.append(gymapi.Vec3(0.2, 0.4-two_robot_offset, 0.2))   # 1, plane vis 2 for med robot course
    # vis_cam_positions.append(gymapi.Vec3(0.15, 0.3-two_robot_offset, 0.2))   # 2
    # vis_cam_positions.append(gymapi.Vec3(0.12, 0.3-two_robot_offset, 0.15))   # 2 bis
    # vis_cam_positions.append(gymapi.Vec3(-0.1, 0.3-two_robot_offset, 0.2))   # 3
    # vis_cam_positions.append(gymapi.Vec3(0.15, 0.2-two_robot_offset, 0.2))   # 4
    # vis_cam_targets.append(gymapi.Vec3(0.0, 0.3-two_robot_offset, 0.05))   #4
    vis_cam_targets.append(gymapi.Vec3(0.0, 0.5-two_robot_offset, 0.05))
    
    for i, env_obj in enumerate(envs_obj):
        # for c in range(len(cam_positions)):
            vis_cam_handles.append(gym.create_camera_sensor(env_obj, vis_cam_props))
            gym.set_camera_location(vis_cam_handles[i], env_obj, vis_cam_positions[0], vis_cam_targets[0])



    # set dof properties
    for env in envs:
        gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)

        

    '''
    Main stuff is here
    '''
    rospy.init_node('isaac_grasp_client')
    rospy.logerr("======Loading object ... " + str(args.obj_name))  
 

    # Some important paramters
    init()  # Initilize 2 robots' joints
    all_done = False
    state = "home"
    

    goal_recording_path = "/home/baothach/shape_servo_data/comparison/RRT/goal_data"
    goal_point_count = 0

    # data_recording_path = "/home/baothach/shape_servo_data/RL_shapeservo/box/data"
    terminate_count = 0
    sample_count = 0
    frame_count = 0
    group_count = 0
    data_point_count = 0
    max_group_count = 10
    max_sample_count = 1
    max_data_point_count = 100
    # if args.obj_name == 'box_64':
    #     max_data_point_per_variation = 9600
    # else:
    #     max_data_point_per_variation = data_point_count + 150
    # rospy.logwarn("max_data_point_per_variation:" + str(max_data_point_per_variation))

    final_point_clouds = []
    final_desired_positions = []
    pc_on_trajectory = []
    full_pc_on_trajectory = []
    poses_on_trajectory = []
    first_time = True
    save_intial_pc = True
    switch = True
    total_computation_time = 0

    dc_client = GraspDataCollectionClient()


    device = torch.device("cuda")
    # set up PointSDF encoder model
    encoder = PointSDFModel().to(device)
    pretrained_dict = torch.load('/home/baothach/ll4ma_3drecon_uois/dependencies/PointSDFPytorch/models/sdf_models/pointsdf_single_object_model.pth')
    encoder.load_state_dict(pretrained_dict)
    encoder.eval()

    # set up transition model
    trans_model = TransitionSDF().to(device)
    pretrained_dict = torch.load('/home/baothach/shape_servo_data/RL_shapeservo/box/weights_SDF_dynamics/run1/epoch 150')
    trans_model.load_state_dict(pretrained_dict)
    trans_model.eval()

    # Set up plane model
    plane_model = PlaneSDF().to(device)
    pretrained_dict = torch.load('/home/baothach/shape_servo_data/RL_shapeservo/box/weights_SDF_plane/run1/epoch 150')
    plane_model.load_state_dict(pretrained_dict)
    plane_model.eval()
    # constrain_plane = np.array([0, 1, 0, 0.52])
    # constrain_plane = np.array([1, 1, 0, 0.40])
    # constrain_plane = np.array([-1, 1, 0, 0.41])
    # constrain_plane = np.array([0.12162162, 1, 0, 0.40989865])
    constrain_plane = np.array([0.27766052, 1., 0., 0.39016108+2.5/100]) 

    # def generate_new_target_plane():
    #     choice = np.random.randint(0,3)
    #     if choice == 0: # horizontal planes
    #         pos = np.random.uniform(low=0.36, high=0.45)
    #         return np.array([0, 1, 0, pos])
    #     elif choice == 1:   # tilted left
    #         pos = np.random.uniform(low=0.33, high=0.42) 
    #         return np.array([1, 1, 0, pos])
    #     elif choice == 2:   # tilted right
    #         pos = np.random.uniform(low=0.34, high=0.45)  
    #         return np.array([-1, 1, 0, pos])

    # constrain_plane = generate_new_target_plane()
    constrain_plane_tensor = torch.from_numpy(constrain_plane).unsqueeze(0).float().to(device)
    success_count = 0
    total_count = 1


    vis_frame_count = 0
    num_image = 0
    start_vis_cam = True
    prepare_vis_cam = True
    save_path = "/home/baothach/shape_servo_data/RL_shapeservo/box/plane_vis/3"


    start_time = timeit.default_timer()    

    close_viewer = False

    robot = Robot(gym, sim, envs[0], kuka_handles_2[0])

    while (not close_viewer) and (not all_done): 



        if not args.headless:
            close_viewer = gym.query_viewer_has_closed(viewer)  

        # step the physics
        gym.simulate(sim)
        gym.fetch_results(sim, True)
 

        if prepare_vis_cam:
            plane_points = visualize_plane(constrain_plane, x_range=[-0.3,0.15], y_range=0.5, z_range=0.15, num_pts=50000)
            # plane_points = visualize_plane(constrain_plane, x_range=[-0.3,0.3], y_range=0.5, z_range=0.1, num_pts=50000)
            plane_xs, plane_ys = get_goal_projected_on_image(plane_points, i, thickness = 0)
            valid_ind = []
            for t in range(len(plane_xs)):
                if 0 < plane_xs[t] < vis_cam_width and 0 < plane_ys[t] < vis_cam_height:
                    valid_ind.append(t)
            plane_xs = np.array(plane_xs)[valid_ind]
            plane_ys = np.array(plane_ys)[valid_ind]
            


            prepare_vis_cam = False

        if start_vis_cam: 
            if vis_frame_count % 20 == 0:
                gym.render_all_camera_sensors(sim)
                im = gym.get_camera_image(sim, envs_obj[i], vis_cam_handles[0], gymapi.IMAGE_COLOR).reshape((vis_cam_height,vis_cam_width,4))
                # goal_xs, goal_ys = get_goal_projected_on_image(data["full pcs"][1], i, thickness = 1)
                
                im[plane_xs, plane_ys, :] = [0,0,255,255]
             


                im = Image.fromarray(im)
                
                img_path =  os.path.join(save_path, "image" + f'{num_image:03}' + ".png")
                
                im.save(img_path)
                num_image += 1         
            
   

            vis_frame_count += 1




        if state == "home" :   
            frame_count += 1
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka", "psm_main_insertion_joint"), 0.103)
            gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_main_insertion_joint"), 0.203)            
            if frame_count == 10:
                rospy.loginfo("**Current state: " + state + ", current sample count: " + str(sample_count))
                

                if first_time:                    
                    gym.refresh_particle_state_tensor(sim)
                    saved_object_state = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim))) 
                    init_robot_state = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_ALL))
                    first_time = False

                state = "generate preshape"
                
                frame_count = 0

                current_pc = get_point_cloud()
                pcd = open3d.geometry.PointCloud()
                pcd.points = open3d.utility.Vector3dVector(np.array(current_pc))
                open3d.io.write_point_cloud("/home/baothach/shape_servo_data/multi_grasps/1.pcd", pcd) # save_grasp_visual_data , point cloud of the object
                pc_ros_msg = dc_client.seg_obj_from_file_client(pcd_file_path = "/home/baothach/shape_servo_data/multi_grasps/1.pcd", align_obj_frame = False).obj
                pc_ros_msg = fix_object_frame(pc_ros_msg)
                saved_object_state = deepcopy(gymtorch.wrap_tensor(gym.acquire_particle_state_tensor(sim))) 


        if state == "generate preshape":                   
            rospy.loginfo("**Current state: " + state)
            preshape_response = dc_client.gen_grasp_preshape_client(pc_ros_msg)               
            cartesian_goal = deepcopy(preshape_response.palm_goal_pose_world[0].pose)        
            target_pose = [-cartesian_goal.position.x, -cartesian_goal.position.y, cartesian_goal.position.z-ROBOT_Z_OFFSET,
                            0, 0.707107, 0.707107, 0]


            mtp_behavior = MoveToPose(target_pose, robot, sim_params.dt, 2)
            if mtp_behavior.is_complete_failure():
                rospy.logerr('Can not find moveit plan to grasp. Ignore this grasp.\n')  
                state = "reset"                
            else:
                rospy.loginfo('Sucesfully found a PRESHAPE moveit plan to grasp.\n')
                state = "move to preshape"
                # rospy.loginfo('Moving to this preshape goal: ' + str(cartesian_goal))


        if state == "move to preshape":         
            action = mtp_behavior.get_action()

            if action is not None:
                gym.set_actor_dof_position_targets(robot.env_handle, robot.robot_handle, action.get_joint_position())      
                        
            if mtp_behavior.is_complete():
                state = "grasp object"   
                rospy.loginfo("Succesfully executed PRESHAPE moveit arm plan. Let's fucking grasp it!!") 

        
        if state == "grasp object":             
            rospy.loginfo("**Current state: " + state)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), -2.5)
            gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), -3.0)         

            g_1_pos = 0.35
            g_2_pos = -0.35
            dof_states = gym.get_actor_dof_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)
            if dof_states['pos'][8] < 0.35:
                                       
                state = "get shape servo plan"
                    
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper1_joint"), g_1_pos)
                gym.set_joint_target_position(envs[i], gym.get_joint_handle(envs[i], "kuka2", "psm_tool_gripper2_joint"), g_2_pos)         
        
                anchor_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
                # print("***Current x, y, z: ", current_pose["pose"]["p"]["x"], current_pose["pose"]["p"]["y"], current_pose["pose"]["p"]["z"] )
                


        if state == "get shape servo plan":
            rospy.loginfo("**Current state: " + state)

            current_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
            print("***Current x, y, z: ", current_pose["pose"]["p"]["x"], current_pose["pose"]["p"]["y"], current_pose["pose"]["p"]["z"] ) 
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_tool_gripper1_joint"), 0.4)
            # gym.set_joint_target_position(envs[0], gym.get_joint_handle(envs[0], "kuka2", "psm_tool_gripper2_joint"), -0.3) 

            # current_pc = get_point_cloud()
            # current_pc = get_point_cloud_2(i)
            # current_pc = get_partial_point_cloud(i)
            current_pc = modify_pc(get_partial_point_cloud(i) + np.array([0.0, 0.42, -0.01818]))
            farthest_indices,_ = farthest_point_sampling(current_pc, 1024)
            current_pc = current_pc[farthest_indices.squeeze()]    
            # assert current_pc.shape[0] == 1024        



            current_pc_tensor = torch.from_numpy(current_pc).unsqueeze(0).float().to(device)
            curr_state = encoder(current_pc_tensor, get_embedding=True)  #(1,256)
            x_range = [-0.1, 0.1]
            y_range = [-0.1, 0.1]
            z_range = [-0.1, 0.1]
            # x_range = [-0.05, 0.05]
            # y_range = [-0.05, 0.05]
            # z_range = [-0.05, 0.05]
            num_pts = 50240
            sampled_actions = np.random.uniform(low=(x_range[0], y_range[0], z_range[0]), \
                                                high=(x_range[1], y_range[1], z_range[1]), size=(num_pts,3))
            sampled_actions_tensor = (torch.from_numpy(sampled_actions).float().to(device))*1000
            curr_states = curr_state.repeat(num_pts,1)
            pred_next_states = trans_model(curr_states, sampled_actions_tensor)
            percent_passed = plane_model(pred_next_states, constrain_plane_tensor.repeat(num_pts,1))
            nearest_idxs = torch.argmax(percent_passed)
            
            # print("nearest action:", sampled_actions[nearest_idxs.flatten()]) 
            desired_position = sampled_actions[nearest_idxs.flatten()].squeeze()
            
            # del current_pc_tensor
            # torch.cuda.empty_cache()



            # pcd = open3d.geometry.PointCloud()
            # pcd.points = open3d.utility.Vector3dVector(current_pc)  
            # # open3d.visualization.draw_geometries([pcd, pcd_goal])  
            # chamfer_dist = np.linalg.norm(np.asarray(pcd_goal.compute_point_cloud_distance(pcd)))
            # print("chamfer distance: ", chamfer_dist)
            # chamfer_distances.append(chamfer_dist)            
            
            
       

            print("from model:", desired_position)
            
            delta_x = desired_position[0]   
            delta_y = desired_position[1] 
            delta_z = max(0.04, desired_position[2])   

            tvc_behavior = TaskVelocityControl([delta_x, delta_y, delta_z], robot, sim_params.dt, 3, vel_limits, error_threshold = 2e-3)     
            dof_props_2['driveMode'][:8].fill(gymapi.DOF_MODE_VEL)
            dof_props_2["stiffness"][:8].fill(0.0)
            dof_props_2["damping"][:8].fill(40.0)
            gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)
            closed_loop_start_time = timeit.default_timer()


            state = "move to goal"
            # traj_index = 0

        if state == "move to goal":           
            print("success count / total count:", success_count, data_point_count)
            if frame_count % 10 == 0:
                gt_percent_passed = passed_plane(constrain_plane, get_point_cloud())
                print("gt percent passed:", gt_percent_passed)
                print("predicted percent passed:", torch.max(percent_passed))
            frame_count += 1

            # if gt_percent_passed >= 0.99:
            #     success_count += 1
            #     state = "reset"
            # elif timeit.default_timer() - closed_loop_start_time >= 120:
            #     rospy.logerr("FAILLLLLLLLLL")
            #     state = "reset" 


            action = tvc_behavior.get_action()  
            # print("time, action, complete:", timeit.default_timer() - closed_loop_start_time, action, mtp_behavior.is_complete())
            if action is None:   
                final_pose = deepcopy(gym.get_actor_rigid_body_states(envs[i], kuka_handles_2[i], gymapi.STATE_POS)[-3])
                # print("***Final x, y, z: ", final_pose["pose"]["p"]["x"], final_pose["pose"]["p"]["y"], final_pose["pose"]["p"]["z"] ) 
                delta_x = -(final_pose["pose"]["p"]["x"] - anchor_pose["pose"]["p"]["x"])
                delta_y = -(final_pose["pose"]["p"]["y"] - anchor_pose["pose"]["p"]["y"])
                delta_z = final_pose["pose"]["p"]["z"] - anchor_pose["pose"]["p"]["z"]
                print("delta x, y, z:", delta_x, delta_y, delta_z)
                state = "get shape servo plan"    
            else:
                gym.set_actor_dof_velocity_targets(robot.env_handle, robot.robot_handle, action.get_joint_position())                
                            


        if state == "reset":   
            rospy.loginfo("**Current state: " + state)
            frame_count = 0
            
            dof_props_2['driveMode'][:8].fill(gymapi.DOF_MODE_POS)
            dof_props_2["stiffness"][:8].fill(200.0)
            dof_props_2["damping"][:8].fill(40.0)
            gym.set_actor_dof_properties(env, kuka_handles_2[i], dof_props_2)
            

            gym.set_actor_rigid_body_states(envs[i], kuka_handles_2[i], init_robot_state, gymapi.STATE_ALL) 
            gym.set_particle_state_tensor(sim, gymtorch.unwrap_tensor(saved_object_state))
            gym.set_actor_dof_velocity_targets(robot.env_handle, robot.robot_handle, [0]*8)



            print("Sucessfully reset robot and object")
            pc_on_trajectory = []
            full_pc_on_trajectory = []
            poses_on_trajectory = []
            data_point_count += 1    
            constrain_plane = generate_new_target_plane()
            constrain_plane_tensor = torch.from_numpy(constrain_plane).unsqueeze(0).float().to(device)
            state = "home"
 
        
        if sample_count == max_sample_count:  
            sample_count = 0            
            group_count += 1
            print("group count: ", group_count)
            state = "record data" 

        if state == "record data":
            frame_count = 0
            sample_count = 0
            rospy.loginfo("succesfully record data")  
            state = "reset"

        # if group_count == max_group_count or data_point_count >= max_data_point_count: 
        if  data_point_count >= max_data_point_count:
                    
            all_done = True 

        # step rendering
        gym.step_graphics(sim)
        if not args.headless:
            gym.draw_viewer(viewer, sim, False)
            # gym.sync_frame_time(sim)


  
    print("total computation time:", total_computation_time)



    print("All done !")
    print("Elapsed time", timeit.default_timer() - start_time)
    if not args.headless:
        gym.destroy_viewer(viewer)
    gym.destroy_sim(sim)
    print("total data pt count: ", data_point_count)
