# Axis Twist Compensation
#
# Copyright (C) 2022  Jeremy Tan <jeremytkw98@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math
from . import manual_probe as ManualProbe, bed_mesh as BedMesh, probe


DEFAULT_SAMPLE_COUNT = 3
DEFAULT_SPEED = 50.0
DEFAULT_HORIZONTAL_MOVE_Z = 5.0


class AxisTwistCompensation:
    def __init__(self, config):
        # get printer
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")

        # get values from [axis_twist_compensation] section in printer .cfg
        self.horizontal_move_z = config.getfloat(
            "horizontal_move_z", DEFAULT_HORIZONTAL_MOVE_Z
        )
        self.speed = config.getfloat("speed", DEFAULT_SPEED)
        self.calibrate_start_x = config.getfloat("calibrate_start_x")
        self.calibrate_end_x = config.getfloat("calibrate_end_x")
        self.calibrate_y = config.getfloat("calibrate_y")
        self.zx_compensations = config.getlists(
            "zx_compensations", default=[], parser=float
        )
        self.compensation_start_x = config.getfloat(
            "compensation_start_x", default=None
        )
        self.compensation_end_x = config.getfloat(
            "compensation_end_x", default=None
        )

        self.calibrate_start_y = config.getfloat(
            'calibrate_start_y', default=None
        )
        self.calibrate_end_y = config.getfloat(
            'calibrate_end_y', default=None
        )
        self.calibrate_x = config.getfloat(
            'calibrate_x', default=None
        )
        self.compensation_start_y = config.getfloat(
            'compensation_start_y', default=None
        )
        self.compensation_end_y = config.getfloat(
            'compensation_end_y', default=None
        )
        self.zy_compensations = config.getlists(
            'zy_compensations', default=[], parser=float
        )
        
        self.m = None
        self.b = None

        # setup calibrater
        self.calibrater = Calibrater(self, config)

        self.printer.register_event_handler("probe:update_results",
                                            self._update_z_compensation_value)

    def _update_z_compensation_value(self, pos):
        if self.zx_compensations:
            pos[2] += self._get_interpolated_z_compensation(
                pos[0], self.zx_compensations,
                self.compensation_start_x,
                self.compensation_end_x
                )

        if self.zy_compensations:
            pos[2] += self._get_interpolated_z_compensation(
                pos[1], self.zy_compensations,
                self.compensation_start_y,
                self.compensation_end_y
                )

    def _get_interpolated_z_compensation(
            self, coord, z_compensations,
            comp_start,
            comp_end
            ):
        
        if not self.zx_compensations:
            return
        
        sample_count = len(z_compensations)
        spacing = ((comp_end - comp_start)
                   / (sample_count - 1))
        interpolate_t = (coord - comp_start) / spacing
        interpolate_i = int(math.floor(interpolate_t))
        interpolate_i = BedMesh.constrain(interpolate_i, 0, sample_count - 2)
        interpolate_t -= interpolate_i
        interpolated_z_compensation = BedMesh.lerp(
            interpolate_t,
            z_compensations[interpolate_i],
            z_compensations[interpolate_i + 1],
        )
        return interpolated_z_compensation

    def clear_compensations(self):
        self.zx_compensations = []
        self.zy_compensations = []
        self.m = None
        self.b = None


class Calibrater:
    def __init__(self, compensation, config):
        # setup self attributes
        self.compensation = compensation
        self.printer = compensation.printer
        self.gcode = self.printer.lookup_object("gcode")
        self.probe = None
        # probe settings are set to none, until they are available
        self.lift_speed, self.probe_x_offset, self.probe_y_offset, _ = (
            None,
            None,
            None,
            None,
        )
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.speed = compensation.speed
        self.horizontal_move_z = compensation.horizontal_move_z
        self.x_start_point = (
            compensation.calibrate_start_x,
            compensation.calibrate_y,
        )
        self.x_end_point = (
            compensation.calibrate_end_x,
            compensation.calibrate_y,
        )
        self.y_start_point = (
            compensation.calibrate_start_y,
            compensation.calibrate_x
        )
        self.y_end_point = (
            compensation.calibrate_end_y,
            compensation.calibrate_x
        )
        self.results = None
        self.current_point_index = None
        self.gcmd = None
        self.configname = config.get_name()

        # register gcode handlers
        self._register_gcode_handlers()

    def _handle_connect(self):
        self.probe = self.printer.lookup_object("probe", None)
        if self.probe is None:
            config = self.printer.lookup_object("configfile")
            raise config.error(
                "AXIS_TWIST_COMPENSATION requires [probe] to be defined"
            )
        self.lift_speed = self.probe.get_lift_speed()
        self.probe_x_offset, self.probe_y_offset, _ = self.probe.get_offsets()

    def _register_gcode_handlers(self):
        # register gcode handlers
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "AXIS_TWIST_COMPENSATION_CALIBRATE",
            self.cmd_AXIS_TWIST_COMPENSATION_CALIBRATE,
            desc=self.cmd_AXIS_TWIST_COMPENSATION_CALIBRATE_help,
        )
        self.gcode.register_command(
            'AXIS_TWIST_COMPENSATION_AUTOCALIBRATE',
            self.cmd_AXIS_TWIST_COMPENSATION_AUTOCALIBRATE,
            desc=self.cmd_AXIS_TWIST_COMPENSATION_CALIBRATE_help)

    cmd_AXIS_TWIST_COMPENSATION_CALIBRATE_help = """
    Performs the twist calibration wizard
    Measure z probe offset at n points along the x or y axis,
    and calculate x or ytwist compensation
    """

    def cmd_AXIS_TWIST_COMPENSATION_CALIBRATE(self, gcmd):
        self.gcmd = gcmd
        sample_count = gcmd.get_int("SAMPLE_COUNT", DEFAULT_SAMPLE_COUNT)
        axis = gcmd.get('AXIS', 'X')

        # check for valid sample_count
        if sample_count is None or sample_count < 2:
            raise self.gcmd.error("SAMPLE_COUNT to probe must be at least 2")

        # clear the current config
        self.compensation.clear_compensations()

        # calculate some values
        if axis == 'X':
            start_point = self.x_start_point
            end_point = self.x_end_point
        elif axis == 'Y':

            if not all([
                self.y_start_point[0],
                self.y_end_point[0],
                self.y_start_point[1]
                ]):
                raise self.gcmd.error(
                    """AXIS_TWIST_COMPENSATION for Y axis requires
                    calibrate_start_y, calibrate_end_y and calibrate_x
                    to be defined
                    """
                    )

            start_point = self.y_start_point
            end_point = self.y_end_point
        else:
            raise self.gcmd.error(
                "AXIS_TWIST_COMPENSATION_CALIBRATE: "
                "Invalid axis.")
            return

        axis_range = end_point[0] - start_point[0]

        interval_dist = axis_range / (sample_count - 1)

        # calculate the points to put the probe at, returned as a list of tuples
        nozzle_points = []

        if axis == 'X':
            for i in range(sample_count):
                x = start_point[0] + i * interval_dist
                y = start_point[1]
                nozzle_points.append((x, y))

        elif axis == 'Y':
            for i in range(sample_count):
                x = start_point[1]
                y = start_point[0] + i * interval_dist
                nozzle_points.append((x, y))

        probe_points = self._calculate_probe_points(
            nozzle_points, self.probe_x_offset, self.probe_y_offset
        )

        # verify no other manual probe is in progress
        ManualProbe.verify_no_manual_probe(self.printer)

        # begin calibration
        self.current_point_index = 0
        self.results = []
        self.current_axis = axis
        self._calibration(probe_points, nozzle_points, interval_dist)

    def _calculate_corrections(self, coordinates):
        # Extracting x, y, and z values from coordinates
        x_coords = [coord[0] for coord in coordinates]
        y_coords = [coord[1] for coord in coordinates]
        z_coords = [coord[2] for coord in coordinates]

        # Calculate the desired point (average of all corner points in z)
        # For a general case, we should extract the unique
        # combinations of corner points
        z_corners = [z_coords[i] for i, coord in enumerate(coordinates)
                     if (coord[0] in [x_coords[0], x_coords[-1]])
                     and (coord[1] in [y_coords[0], y_coords[-1]])]
        z_desired = sum(z_corners) / len(z_corners)


        # Calculate average deformation per axis
        unique_x_coords = sorted(set(x_coords))
        unique_y_coords = sorted(set(y_coords))

        avg_z_x = []
        for x in unique_x_coords:
            indices = [i for i, coord in enumerate(coordinates)
                       if coord[0] == x]
            avg_z = sum(z_coords[i] for i in indices) / len(indices)
            avg_z_x.append(avg_z)

        avg_z_y = []
        for y in unique_y_coords:
            indices = [i for i, coord in enumerate(coordinates)
                       if coord[1] == y]
            avg_z = sum(z_coords[i] for i in indices) / len(indices)
            avg_z_y.append(avg_z)

        # Calculate corrections to reach the desired point
        x_corrections = [z_desired - avg for avg in avg_z_x]
        y_corrections = [z_desired - avg for avg in avg_z_y]

        return x_corrections, y_corrections

    def cmd_AXIS_TWIST_COMPENSATION_AUTOCALIBRATE(self, gcmd):
        self.gcmd = gcmd
        sample_count = gcmd.get_int('SAMPLE_COUNT', DEFAULT_SAMPLE_COUNT)


        if not all([
                self.x_start_point[0],
                self.x_end_point[0],
                self.y_start_point[0],
                self.y_end_point[0]
                ]):
                raise self.gcmd.error(
                    """AXIS_TWIST_COMPENSATION_AUTOCALIBRATE requires
                    calibrate_start_x, calibrate_end_x, calibrate_start_y
                    and calibrate_end_y to be defined
                    """
                    )

        # check for valid sample_count
        if sample_count is None or sample_count < 2:
            raise self.gcmd.error(
                "SAMPLE_COUNT to probe must be at least 2")

        # clear the current config
        self.compensation.clear_compensations()

        # calculate the points to put the probe at, returned as a list of tuples
        nozzle_points = []

        min_x = self.x_start_point[0]
        max_x = self.x_end_point[0]
        min_y = self.y_start_point[0]
        max_y = self.y_end_point[0]

        # calculate x positions
        spcx = (max_x - min_x) / (sample_count - 1)
        xps = [min_x + spcx * i for i in range(sample_count)]

        # Calculate points array
        spcy = (max_y - min_y) / (sample_count - 1)
        flip = False

        points = []
        for i in range(sample_count):
            for j in range(sample_count):
                if(not flip):
                    idx = j
                else:
                    idx = sample_count -1 - j
                points.append([xps[i], min_y + spcy * idx ])
            flip = not flip

        # verify no other manual probe is in progress
        ManualProbe.verify_no_manual_probe(self.printer)

        # calculate the points to put the nozzle at, and probe
        probe_points = []

        for i in range(len(points)):
            x = points[i][0] - self.probe_x_offset
            y = points[i][1] - self.probe_y_offset
            probe_points.append([x, y, self._auto_calibration((x,y))[2]])

        # calculate corrections
        x_corr, y_corr = self._calculate_corrections(probe_points)

        x_corr_str = ', '.join(["{:.6f}".format(x)
                                   for x in x_corr])

        y_corr_str = ', '.join(["{:.6f}".format(x)
                                   for x in y_corr])

        # finalize
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.configname, 'zx_compensations', x_corr_str)
        configfile.set(self.configname, 'compensation_start_x',
                    self.x_start_point[0])
        configfile.set(self.configname, 'compensation_end_x',
                    self.x_end_point[0])


        configfile.set(self.configname, 'zy_compensations', y_corr_str)
        configfile.set(self.configname, 'compensation_start_y',
                    self.y_start_point[0])
        configfile.set(self.configname, 'compensation_end_y',
                    self.y_end_point[0])


        self.gcmd.respond_info(
            "AXIS_TWIST_COMPENSATION_AUTOCALIBRATE: Calibration complete: ")
        self.gcmd.respond_info("\n".join(map(str, [x_corr, y_corr])), log=False)

    def _auto_calibration(self, probe_point):

        # horizontal_move_z (to prevent probe trigger or hitting bed)
        self._move_helper((None, None, self.horizontal_move_z))

        # move to point to probe
        self._move_helper((probe_point[0],
                           probe_point[1], None))

        # probe the point
        pos = probe.PrinterProbe.run_probe(self.probe, self.gcmd)
        #self.current_measured_z = pos[2]

        # horizontal_move_z (to prevent probe trigger or hitting bed)
        self._move_helper((None, None, self.horizontal_move_z))

        return pos

    def _calculate_probe_points(
        self, nozzle_points, probe_x_offset, probe_y_offset
    ):
        # calculate the points to put the nozzle at
        # returned as a list of tuples
        probe_points = []
        for point in nozzle_points:
            x = point[0] - probe_x_offset
            y = point[1] - probe_y_offset
            probe_points.append((x, y))
        return probe_points

    def _move_helper(self, target_coordinates, override_speed=None):
        # pad target coordinates
        target_coordinates = (
            (target_coordinates[0], target_coordinates[1], None)
            if len(target_coordinates) == 2
            else target_coordinates
        )
        toolhead = self.printer.lookup_object("toolhead")
        speed = self.speed if target_coordinates[2] is None else self.lift_speed
        speed = override_speed if override_speed is not None else speed
        toolhead.manual_move(target_coordinates, speed)

    def _calibration(self, probe_points, nozzle_points, interval):
        # begin the calibration process
        self.gcmd.respond_info(
            "AXIS_TWIST_COMPENSATION_CALIBRATE: "
            "Probing point %d of %d"
            % (self.current_point_index + 1, len(probe_points))
        )

        # horizontal_move_z (to prevent probe trigger or hitting bed)
        self._move_helper((None, None, self.horizontal_move_z))

        # move to point to probe
        self._move_helper(
            (
                probe_points[self.current_point_index][0],
                probe_points[self.current_point_index][1],
                None,
            )
        )

        # probe the point
        self.current_measured_z = self.probe.run_probe(self.gcmd)[2]

        # horizontal_move_z (to prevent probe trigger or hitting bed)
        self._move_helper((None, None, self.horizontal_move_z))

        # move the nozzle over the probe point
        self._move_helper((nozzle_points[self.current_point_index]))

        # start the manual (nozzle) probe
        ManualProbe.ManualProbeHelper(
            self.printer,
            self.gcmd,
            self._manual_probe_callback_factory(
                probe_points, nozzle_points, interval
            ),
        )

    def _manual_probe_callback_factory(
        self, probe_points, nozzle_points, interval
    ):
        # returns a callback function for the manual probe
        is_end = self.current_point_index == len(probe_points) - 1

        def callback(kin_pos):
            if kin_pos is None:
                # probe was cancelled
                self.gcmd.respond_info(
                    "AXIS_TWIST_COMPENSATION_CALIBRATE: Probe cancelled, "
                    "calibration aborted"
                )
                return
            z_offset = self.current_measured_z - kin_pos[2]
            self.results.append(z_offset)
            if is_end:
                # end of calibration
                self._finalize_calibration()
            else:
                # move to next point
                self.current_point_index += 1
                self._calibration(probe_points, nozzle_points, interval)

        return callback

    def _finalize_calibration(self):
        # finalize the calibration process
        # calculate average of results
        avg = sum(self.results) / len(self.results)
        # subtract average from each result
        # so that they are independent of z_offset
        self.results = [avg - x for x in self.results]
        # save the config
        configfile = self.printer.lookup_object("configfile")
        values_as_str = ", ".join(["{:.6f}".format(x) for x in self.results])
        if(self.current_axis == 'X'):

            configfile.set(self.configname, 'zx_compensations', values_as_str)
            configfile.set(self.configname, 'compensation_start_x',
                        self.x_start_point[0])
            configfile.set(self.configname, 'compensation_end_x',
                        self.x_end_point[0])

            self.compensation.zx_compensations = self.results
            self.compensation.compensation_start_x = self.x_start_point[0]
            self.compensation.compensation_end_x = self.x_end_point[0]

        elif(self.current_axis == 'Y'):

            configfile.set(self.configname, 'zy_compensations', values_as_str)
            configfile.set(self.configname, 'compensation_start_y',
                        self.y_start_point[0])
            configfile.set(self.configname, 'compensation_end_y',
                        self.y_end_point[0])

            self.compensation.zy_compensations = self.results
            self.compensation.compensation_start_y = self.y_start_point[0]
            self.compensation.compensation_end_y = self.y_end_point[0]

        self.gcode.respond_info(
            "AXIS_TWIST_COMPENSATION state has been saved "
            "for the current session.  The SAVE_CONFIG command will "
            "update the printer config file and restart the printer."
        )
        # output result
        self.gcmd.respond_info(
            "AXIS_TWIST_COMPENSATION_CALIBRATE: Calibration complete, "
            "offsets: %s, mean z_offset: %f" % (self.results, avg)
        )


# klipper's entry point using [axis_twist_compensation] section in printer.cfg
def load_config(config):
    return AxisTwistCompensation(config)
