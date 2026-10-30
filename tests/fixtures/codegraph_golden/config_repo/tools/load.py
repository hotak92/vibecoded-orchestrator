"""Loads the vehicle parameters."""
import configparser

parser = configparser.ConfigParser()
GGV = parser.get('GENERAL_OPTIONS', 'ggv_file')


def load():
    return open("params/vehicle.ini").read()


def mass(cfg):
    return cfg["veh_params"]["mass_kg"]
