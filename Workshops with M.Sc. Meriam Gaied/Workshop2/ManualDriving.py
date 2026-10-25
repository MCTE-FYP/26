import csv
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
import time

import carla
import pygame

logging.basicConfig(
    filename=str(Path(__file__).with_name("data.txt")),
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger(__name__)

STEERING_AXIS = 0
THROTTLE_AXIS = 2
BRAKE_AXIS = 5
REVERSE_BUTTON = 21
HANDBRAKE_BUTTON = 23

FIXED_DELTA_SECONDS = 1.0 / 120.0
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
IMAGE_CAPTURE_RATE_HZ = 10
IMAGE_QUEUE_SIZE = 20
SAVE_CAMERA_IMAGES = True
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
IMAGE_DIRECTORY = Path(__file__).with_name("CameraImages") / f"run_{RUN_TIMESTAMP}"
MOTION_CUEING_FILE = Path(__file__).with_name(f"motion_cueing_{RUN_TIMESTAMP}.csv")
MOTION_CUEING_COLUMNS = (
    "acceleration_x",
    "acceleration_y",
    "acceleration_z",
    "angular_velocity_x",
    "angular_velocity_y",
    "angular_velocity_z",
)


def update_spectator_view(spectator, vehicle_transform):
    camera_location = vehicle_transform.transform(
        carla.Location(x=0.1, y=-0.35, z=1.1)
    )
    spectator.set_transform(
        carla.Transform(camera_location, vehicle_transform.rotation)
    )


class CameraRecorder:
    def __init__(self):
        self.sensor = None
        self.writer = None
        self.images = queue.Queue(maxsize=IMAGE_QUEUE_SIZE)
        self.errors = []
        self.dropped = 0

    def start(self, world, vehicle):
        if not SAVE_CAMERA_IMAGES:
            return

        blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
        blueprint.set_attribute("image_size_x", str(CAMERA_WIDTH))
        blueprint.set_attribute("image_size_y", str(CAMERA_HEIGHT))
        blueprint.set_attribute("fov", "90")
        blueprint.set_attribute("sensor_tick", str(1.0 / IMAGE_CAPTURE_RATE_HZ))
        self.sensor = world.spawn_actor(
            blueprint,
            carla.Transform(
                carla.Location(x=0.1, y=-0.35, z=1.1),
                carla.Rotation(pitch=0, yaw=0),
            ),
            attach_to=vehicle,
        )
        IMAGE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.writer = threading.Thread(target=self._save_images, daemon=True)
        self.writer.start()
        self.sensor.listen(self._queue_image)
        logger.info("Saving camera images at up to %s Hz to %s",
                    IMAGE_CAPTURE_RATE_HZ, IMAGE_DIRECTORY)

    def _queue_image(self, image):
        try:
            self.images.put_nowait(image)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("Dropped %s camera images: save queue is full",
                               self.dropped)

    def _save_images(self):
        while True:
            image = self.images.get()
            try:
                if image is None:
                    return
                image.save_to_disk(
                    str(IMAGE_DIRECTORY / f"frame_{image.frame:08d}.png")
                )
            except Exception as exc:
                self.errors.append(exc)
                logger.exception("Failed to save camera frame %s", image.frame)
            finally:
                self.images.task_done()

    def check(self):
        if self.errors:
            raise RuntimeError("Camera image writer failed") from self.errors[0]

    def close(self):
        try:
            if self.sensor is not None:
                try:
                    self.sensor.stop()
                finally:
                    self.sensor.destroy()
        finally:
            if self.writer is not None:
                self.images.put(None)
                self.writer.join()


pygame.init()
pygame.joystick.init()

vehicle = None
camera_recorder = None
motion_log_file = None
world = None
original_settings = None
try:
    if pygame.joystick.get_count() == 0:
        raise RuntimeError("No joystick detected")

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    if joystick.get_numaxes() <= max(STEERING_AXIS, THROTTLE_AXIS, BRAKE_AXIS):
        raise RuntimeError(f"{joystick.get_name()} does not have the required axes")
    if joystick.get_numbuttons() <= max(REVERSE_BUTTON, HANDBRAKE_BUTTON):
        raise RuntimeError(
            f"{joystick.get_name()} has {joystick.get_numbuttons()} buttons; "
            "buttons 22 and 23 are required"
        )

    pygame.event.pump()
    logger.info(
        "Using %s: %s axes, %s buttons, %s hats",
        joystick.get_name(),
        joystick.get_numaxes(),
        joystick.get_numbuttons(),
        joystick.get_numhats(),
    )

    client = carla.Client("127.0.0.1", 2000)
    client.set_timeout(10.0)
    world = client.get_world()
    original_settings = world.get_settings()
    synchronous_settings = world.get_settings()
    synchronous_settings.synchronous_mode = True
    synchronous_settings.fixed_delta_seconds = FIXED_DELTA_SECONDS
    world.apply_settings(synchronous_settings)

    blueprints = world.get_blueprint_library()
    blueprint = blueprints.find("vehicle.tesla.model3")

    for spawn_point in world.get_map().get_spawn_points():
        vehicle = world.try_spawn_actor(blueprint, spawn_point)
        if vehicle is not None:
            break
    if vehicle is None:
        raise RuntimeError("Could not find a free vehicle spawn point")

    spectator = world.get_spectator()
    update_spectator_view(spectator, vehicle.get_transform())
    logger.info("Using CARLA spectator view; no Pygame display window")

    camera_recorder = CameraRecorder()
    camera_recorder.start(world, vehicle)

    motion_log_file = MOTION_CUEING_FILE.open("w", newline="", encoding="utf-8")
    motion_log = csv.writer(motion_log_file)
    motion_log.writerow(MOTION_CUEING_COLUMNS)
    logger.info("Writing motion cueing data to %s", MOTION_CUEING_FILE)

    handbrake = False
    last_log = time.monotonic()
    while True:
        for event in pygame.event.get():
            if (
                event.type == pygame.JOYBUTTONDOWN
                and event.instance_id == joystick.get_instance_id()
                and event.button == HANDBRAKE_BUTTON
            ):
                handbrake = not handbrake
                logger.info("Handbrake: %s", handbrake)

        steering = max(-1.0, min(1.0, joystick.get_axis(STEERING_AXIS)))
        throttle = max(0.0, min(1.0, joystick.get_axis(THROTTLE_AXIS)))
        brake = max(0.0, min(1.0, joystick.get_axis(BRAKE_AXIS)))
        reverse = bool(joystick.get_button(REVERSE_BUTTON))

        camera_recorder.check()

        vehicle.apply_control(
            carla.VehicleControl(
                steer=steering,
                throttle=throttle,
                brake=brake,
                reverse=reverse,
                hand_brake=handbrake,
            )
        )

        world.tick()
        transform = vehicle.get_transform()
        update_spectator_view(spectator, transform)
        acceleration = vehicle.get_acceleration()
        angular_velocity = vehicle.get_angular_velocity()
        motion_log.writerow(
            (
                acceleration.x,
                acceleration.y,
                acceleration.z,
                angular_velocity.x,
                angular_velocity.y,
                angular_velocity.z,
            )
        )

        now = time.monotonic()
        if now - last_log >= 1.0:
            velocity = vehicle.get_velocity()
            logger.info(
                "Velocity=(%.2f, %.2f, %.2f) steer=%.2f throttle=%.2f brake=%.2f "
                "reverse=%s handbrake=%s",
                velocity.x,
                velocity.y,
                velocity.z,
                steering,
                throttle,
                brake,
                reverse,
                handbrake,
            )
            motion_log_file.flush()
            last_log = now
finally:
    try:
        if motion_log_file is not None:
            motion_log_file.close()
    finally:
        try:
            if camera_recorder is not None:
                camera_recorder.close()
        finally:
            try:
                if vehicle is not None:
                    vehicle.destroy()
            finally:
                try:
                    if world is not None and original_settings is not None:
                        world.apply_settings(original_settings)
                finally:
                    pygame.quit()
