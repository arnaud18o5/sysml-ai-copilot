package VehicleFuelSystem {
    port def FuelPort;

    attribute def Pressure;

    metadata def Critical;

    calc def ComputeMargin {
        in cap : Real;
        in reserve : Real;

        cap - reserve
    }

    part def Engine {
        port fuelIn : FuelPort;
    }

    part def FuelTank {
        port fuelOut : FuelPort;
        attribute capacity : Real = 60;
        attribute fuelType : String = "diesel";
        attribute calibrationPoints : Real = (25, 50, 75, 100);
    }

    part def FuelPump {
        port fuelIn : FuelPort;
        port fuelOut : FuelPort;
        attribute outputPressure : Pressure = 3.5;
    }

    part def Vehicle {
        part engine : Engine;
        part tank : FuelTank;
        part pump : FuelPump;

        connect tank.fuelOut to pump.fuelIn;
        #Critical connect pump.fuelOut to engine.fuelIn;

        attribute capacityMargin : Real = ComputeMargin(tank.capacity, 10);

        constraint {
            tank.capacity > 0 and capacityMargin > -10
        }
    }

    part businessCase {
        part lead : Engine;
    }

    requirement def FuelDeliveryRequirement {
        doc /* The system shall deliver fuel to the engine within 2 seconds of ignition. */
    }

    requirement def FuelPurityRequirement {
        doc /* The fuel delivered to the engine shall be free of contaminants above 10ppm. */
    }

    case def Diagnose {
        doc /* Investigates a reported fuel system fault on a given vehicle. */

        in vehicle : Vehicle;
        in reportedIssue : String;

        constraint {
            vehicle.tank.capacity > 0 and vehicle.capacityMargin > -10
        }
    }

    verification def VerifyCapacity :> Diagnose {
        doc /* Confirms the fuel tank still holds its rated capacity after a diagnostic case. */
    }

    #Critical satisfy FuelDeliveryRequirement by Vehicle::pump;
    satisfy FuelPurityRequirement by Vehicle::tank;
}
