import launch

if not launch.is_installed("matplotlib"):
    launch.run_pip("install matplotlib", "requirements for depthmap script")
    
if not launch.is_installed("numba"):
    launch.run_pip("install numba", "requirements for depthmap script")

#if not launch.is_installed("vispy"):
#    launch.run_pip("install vispy", "requirements for depthmap script")
#if not launch.is_installed("moviepy"):
#    launch.run_pip("install moviepy", "requirements for depthmap script")
#if not launch.is_installed("transforms3d"):
#    launch.run_pip("install transforms3d", "requirements for depthmap script")
