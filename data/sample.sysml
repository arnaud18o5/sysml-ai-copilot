package VehicleFuelSystem {

    port def FuelPort;

    attribute def Pressure;

    part def Engine {
        port fuelIn : FuelPort;
    }

    part def FuelTank {
        port fuelOut : FuelPort;
        attribute capacity : Real = 60;
        attribute fuelType : String = "diesel";
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
        connect pump.fuelOut to engine.fuelIn;
    }

    requirement def FuelDeliveryRequirement {
        doc /* The system shall deliver fuel to the engine within 2 seconds of ignition. */

        requirement def ResponseTimeRequirement {
            doc /* The fuel pump shall reach full flow within 2 seconds of ignition. */
        }
    }

    requirement def FuelPurityRequirement {
        doc /* The fuel delivered to the engine shall be free of contaminants above 10ppm. */
    }

    satisfy FuelDeliveryRequirement by pump;
    satisfy FuelPurityRequirement by tank;

}
